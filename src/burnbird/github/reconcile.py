"""The hourly reconciliation: make the stored installations and repositories match GitHub's lists.

Webhooks can be lost, and some changes send none that removes anything: when an installation
moves from all repositories to selected ones, ``repositories_removed`` "is empty"
(https://docs.github.com/en/webhooks/webhook-events-and-payloads#installation_repositories). So
every hour the App's installations (``GET /app/installations``) and each installation's
repositories (``GET /installation/repositories``) are listed in full, and the stored rows follow.

These rules keep a deletion safe:
- **Snapshot first.** The stored IDs are read before GitHub is asked, and only those can be
  deleted: a row stored while the list was being fetched (by a webhook or a sign-in) is newer than
  the list, so it stays. For the same reason a listed installation deleted during the fetch is not
  stored again, and one whose account has another installation stored is not stored at all:
  storing it would uninstall the other one outside these rules. Repositories follow the same rule:
  one removed from the installation or moved to another during the fetch is not stored again, and
  an installation uninstalled during its repository fetch gets nothing written.
- **Every page first.** A list is collected in full before anything is written; a failure on any
  page changes nothing.
- **Ask before uninstalling.** Lists are paged by number, so an item can move onto a page already
  read and look missing. An installation is uninstalled only when ``GET /app/installations/{id}``
  answers 404; a repository list whose items differ from its ``total_count`` removes nothing.

The mass-deletion guard: uninstalling and removing delete data for good, so a list that would
delete too much deletes nothing, logs an ERROR with counts only, and the next run tries again.
- An empty installation list uninstalls nothing.
- More than ``max(2, 10% of the stored installations, rounded down)`` to uninstall uninstalls
  none.
- An empty repository list while repositories are stored removes none: GitHub requires at least
  one selected repository.
The creates and updates of the same run still apply.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import httpx2
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.github import schedule, schemas
from burnbird.github.client import (
    GithubAnswerError,
    GithubApp,
    InstallationRefusedError,
    RateLimitedError,
)
from burnbird.github.events import store_installation, store_repository
from burnbird.models import Installation, Repository

logger = logging.getLogger(__name__)

TASK: Final = "reconcile"
INTERVAL: Final = timedelta(hours=1)
# The guard: a run uninstalls at most this many, or this share of the stored ones if larger.
MIN_UNINSTALL_LIMIT: Final = 2
UNINSTALL_SHARE_DIVISOR: Final = 10


class _Repositories(schemas.Payload):
    total_count: int
    repositories: tuple[schemas.Repository, ...]


@dataclass(frozen=True, slots=True)
class _Listed:
    """GitHub's installation list: every item's raw ID, and the items that parse."""

    ids: frozenset[int]
    installations: tuple[schemas.Installation, ...]
    skipped: int


def run(sessions: sessionmaker[Session], github: GithubApp, now: datetime) -> bool:
    """Run the reconciliation if it is due; return whether a run was claimed."""
    claimed = schedule.claim(sessions, TASK, now, INTERVAL)
    if claimed is None:
        return False
    try:
        complete, resume_at = reconcile(sessions, github, now)
    except RateLimitedError as err:
        logger.warning("reconcile: the installation list is rate limited; put off")
        schedule.finish(sessions, claimed, complete=False, resume_at=err.resume_at)
        return True
    schedule.finish(sessions, claimed, complete=complete, resume_at=resume_at)
    return True


def reconcile(
    sessions: sessionmaker[Session], github: GithubApp, now: datetime
) -> tuple[bool, datetime | None]:
    """Reconcile installations, then each installation's repositories on its own.

    Returns whether everything was reconciled (no installation skipped, no guard held back), and
    the latest time a rate limit allows the skipped work to resume, if any. A failure of the
    installation list raises and changes nothing.
    """
    with sessions.begin() as session:
        stored = frozenset(session.scalars(select(Installation.id)))
    listed = _list_installations(github)
    gone, complete = _gone(github, stored, listed)
    written, stored_all = _store_installations(sessions, stored, listed, gone)
    complete = complete and stored_all
    resume_at: datetime | None = None
    for installation in listed.installations:
        if installation.suspended_at is not None or installation.id not in written:
            continue
        try:
            complete = _reconcile_repositories(sessions, github, installation.id, now) and complete
        except InstallationRefusedError:
            logger.warning(
                "reconcile: installation %d refused a token (suspended?); skipped",
                installation.id,
            )
            complete = False
        except RateLimitedError as err:
            logger.warning("reconcile: installation %d is rate limited; skipped", installation.id)
            resume_at = err.resume_at if resume_at is None else max(resume_at, err.resume_at)
            complete = False
        except Exception:
            # One installation's failure never stops the others; nothing of it was written.
            logger.exception("reconcile: installation %d failed; skipped", installation.id)
            complete = False
    return complete, resume_at


def _list_installations(github: GithubApp) -> _Listed:
    pages = list(github.pages("app/installations"))
    ids: set[int] = set()
    installations: list[schemas.Installation] = []
    skipped = 0
    for page in pages:
        for item in _items(page):
            ids.add(item["id"])
            try:
                installations.append(schemas.Installation.model_validate(item))
            except ValidationError:
                # An enterprise account, which Burnbird does not handle: listed, so never deleted.
                skipped += 1
    return _Listed(frozenset(ids), tuple(installations), skipped)


def _items(page: Any) -> Sequence[dict[str, Any]]:
    """Return a page's items, each with an integer ID, or raise: without IDs no diff is safe."""
    if not isinstance(page, list) or not all(
        isinstance(item, dict) and type(item.get("id")) is int for item in page
    ):
        raise GithubAnswerError("GET /app/installations answered an item without an ID")
    return page


def _gone(
    github: GithubApp, stored: frozenset[int], listed: _Listed
) -> tuple[frozenset[int], bool]:
    """Return the stored installations to uninstall, and whether none was held back.

    The guard runs first, so a list that would delete too much costs no further call. Each one
    left is uninstalled only when GitHub answers 404 for it; any other answer keeps it.
    """
    missing = stored - listed.ids
    if missing and not listed.ids:
        logger.error(
            "reconcile: GitHub lists no installation, %d stored; none uninstalled", len(stored)
        )
        return frozenset(), False
    limit = max(MIN_UNINSTALL_LIMIT, len(stored) // UNINSTALL_SHARE_DIVISOR)
    if len(missing) > limit:
        logger.error(
            "reconcile: %d of %d stored installations are not listed, more than the limit "
            "of %d; none uninstalled",
            len(missing),
            len(stored),
            limit,
        )
        return frozenset(), False
    gone: set[int] = set()
    complete = True
    for installation_id in sorted(missing):
        try:
            list(github.pages(f"app/installations/{installation_id}"))
        except RateLimitedError:
            raise
        except GithubAnswerError as err:
            if err.status == httpx2.codes.NOT_FOUND:
                gone.add(installation_id)
                continue
            logger.warning(
                "reconcile: installation %d is not listed and could not be checked; kept",
                installation_id,
            )
        else:
            logger.warning(
                "reconcile: installation %d is installed but was not listed; kept", installation_id
            )
        complete = False
    return frozenset(gone), complete


def _store_installations(
    sessions: sessionmaker[Session],
    stored: frozenset[int],
    listed: _Listed,
    gone: frozenset[int],
) -> tuple[frozenset[int], bool]:
    """Uninstall the ``gone`` installations, then store the listed ones that are safe to store.

    Returns the installations stored or updated, and whether every listed one was.
    """
    written: set[int] = set()
    with sessions.begin() as session:
        for installation_id in sorted(gone):
            retention.uninstall(session, installation_id)
        rows = session.execute(select(Installation.id, Installation.account_id))
        present = {installation_id: account_id for installation_id, account_id in rows}
        accounts = {account_id: installation_id for installation_id, account_id in present.items()}
        for installation in listed.installations:
            other = accounts.get(installation.account.id, installation.id)
            if installation.id in stored and installation.id not in present:
                logger.warning(
                    "reconcile: installation %d was deleted while it was listed; not stored",
                    installation.id,
                )
                continue
            if other != installation.id:
                logger.warning(
                    "reconcile: installation %d not stored: its account has %d stored",
                    installation.id,
                    other,
                )
                continue
            store_installation(session, installation)
            session.execute(
                update(Installation)
                .where(Installation.id == installation.id)
                .values(suspended=installation.suspended_at is not None)
            )
            written.add(installation.id)
    logger.info(
        "reconcile: %d installations listed (%d skipped), %d uninstalled",
        len(listed.ids),
        listed.skipped,
        len(gone),
    )
    return frozenset(written), len(written) == len(listed.installations)


def _reconcile_repositories(
    sessions: sessionmaker[Session], github: GithubApp, installation_id: int, now: datetime
) -> bool:
    """Make one installation's stored repositories match GitHub's list; False if held back."""
    with sessions.begin() as session:
        stored = frozenset(
            session.scalars(
                select(Repository.id).where(Repository.installation_id == installation_id)
            )
        )
    pages = list(github.pages("installation/repositories", installation_id))
    try:
        answers = [_Repositories.model_validate(page) for page in pages]
    except ValidationError:
        # Pydantic's message would repeat the values; the call is enough.
        raise GithubAnswerError(
            "GET /installation/repositories answered an item not as documented"
        ) from None
    listed = [repository for answer in answers for repository in answer.repositories]
    if any(answer.total_count != len(listed) for answer in answers):
        # An item moved between pages while they were read: the list cannot be trusted.
        raise GithubAnswerError(
            f"GET /installation/repositories listed {len(listed)} of "
            f"{answers[0].total_count if answers else 0}"
        )
    gone = stored - {repository.id for repository in listed}
    with sessions.begin() as session:
        # The same rule as for installations: a row deleted or moved during the fetch is newer
        # than the list, so the list does not bring it back.
        if (
            session.scalar(select(Installation.id).where(Installation.id == installation_id))
            is None
        ):
            logger.warning(
                "reconcile: installation %d was uninstalled while its repositories were listed; "
                "nothing written",
                installation_id,
            )
            return False
        present = frozenset(
            session.scalars(
                select(Repository.id).where(Repository.installation_id == installation_id)
            )
        )
        changed = sorted(r.id for r in listed if r.id in stored and r.id not in present)
        if changed:
            logger.warning(
                "reconcile: installation %d: repositories %s changed while listed; not stored",
                installation_id,
                changed,
            )
        for repository in listed:
            if repository.id not in changed:
                store_repository(session, installation_id, repository)
        if gone and not listed:
            logger.error(
                "reconcile: installation %d lists no repository, %d stored; none removed",
                installation_id,
                len(stored),
            )
            return False
        retention.remove_repositories(session, installation_id, gone)
        session.execute(
            update(Installation)
            .where(Installation.id == installation_id)
            .values(repositories_synced_at=now)
        )
    return not changed
