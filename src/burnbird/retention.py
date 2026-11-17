"""What is deleted, and when: retention, "Delete my data", revoked sign-ins, uninstalls, removals.

Pure functions over a Session. They never commit: the caller runs each in one transaction, so a
failure deletes nothing. Children are deleted before their parents, so every count is exact; the
tables' ON DELETE CASCADE keys are only the safety net. ``run_purges`` runs every time-based
purge; the worker's daily ``burnbird.purge`` task calls it.
"""

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Literal, cast

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Delete,
    Select,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Session

from burnbird.models import (
    Account,
    Backfill,
    EmailConsent,
    EmailPreference,
    GithubUser,
    Installation,
    Job,
    LabelSize,
    Repository,
    UserEmail,
    UserInstallation,
    UserRepository,
    UserSession,
    WebhookDelivery,
)

logger = logging.getLogger(__name__)

JOB_RETENTION: Final = timedelta(days=90)
DELIVERY_RETENTION: Final = timedelta(days=7)
# An email address not read again at a sign-in within this long is deleted.
EMAIL_FRESHNESS: Final = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Deleted:
    """How many rows of each table one deletion removed."""

    jobs: int = 0
    backfills: int = 0
    repositories: int = 0
    label_sizes: int = 0
    user_installations: int = 0
    user_repositories: int = 0
    installations: int = 0
    accounts: int = 0
    sessions: int = 0
    user_emails: int = 0
    email_preferences: int = 0
    email_consents: int = 0
    users: int = 0


@dataclass(frozen=True, slots=True)
class Purged:
    """How many rows each time-based purge removed."""

    jobs: int
    deliveries: int
    sessions: int
    emails: int


def run_purges(session: Session, now: datetime) -> Purged:
    """Run every time-based purge in the caller's transaction, and log one line of counts."""
    jobs = purge_jobs(session, now)
    deliveries = purge_deliveries(session, now)
    # The owners' user rows before their sessions and addresses, in ID order, in the lock order
    # (``lock_repositories``): a sign-in or Delete my data that holds one is waited for. FOR NO
    # KEY UPDATE, as ``_user_rows`` takes it.
    session.execute(
        select(GithubUser.id)
        .where(
            or_(
                GithubUser.id.in_(select(UserSession.user_id).where(_expired(now))),
                GithubUser.id.in_(select(UserEmail.user_id).where(_stale(now))),
            )
        )
        .order_by(GithubUser.id)
        .with_for_update(key_share=True)
    ).all()
    purged = Purged(
        jobs=jobs,
        deliveries=deliveries,
        sessions=purge_sessions(session, now),
        emails=purge_stale_emails(session, now),
    )
    logger.info(
        "purge: %d jobs, %d deliveries, %d sessions, %d email addresses",
        purged.jobs,
        purged.deliveries,
        purged.sessions,
        purged.emails,
    )
    return purged


def purge_jobs(session: Session, now: datetime) -> int:
    """Delete the jobs created more than 90 days before ``now``; return how many.

    Their repositories are locked first, FOR KEY SHARE, in the lock order (``lock_repositories``):
    every other path that locks a job holds its repository FOR UPDATE first, so it is waited for.
    """
    cutoff = _aware(now) - JOB_RETENTION
    lock_repositories(
        session, select(Job.repository_id).where(Job.created_at < cutoff), lock="KEY SHARE"
    )
    return _count(session, delete(Job).where(Job.created_at < cutoff))


def purge_deliveries(session: Session, now: datetime) -> int:
    """Delete the webhook deliveries received more than 7 days before ``now``; return how many."""
    cutoff = _aware(now) - DELIVERY_RETENTION
    return _count(session, delete(WebhookDelivery).where(WebhookDelivery.received_at < cutoff))


def purge_sessions(session: Session, now: datetime) -> int:
    """Delete the sessions that have expired at ``now``; return how many."""
    return _count(session, delete(UserSession).where(_expired(now)))


def purge_stale_emails(session: Session, now: datetime) -> int:
    """Delete the addresses no sign-in read in the 30 days before ``now``; return how many."""
    return _count(session, delete(UserEmail).where(_stale(now)))


def _expired(now: datetime) -> ColumnElement[bool]:
    return UserSession.expires_at <= _aware(now)


def _stale(now: datetime) -> ColumnElement[bool]:
    return UserEmail.read_at < _aware(now) - EMAIL_FRESHNESS


# PostgreSQL's row-level lock modes that ``lock_repositories`` takes, weakest last; FOR UPDATE
# conflicts with all three, FOR NO KEY UPDATE with itself and FOR UPDATE, FOR KEY SHARE with FOR
# UPDATE only (https://www.postgresql.org/docs/18/explicit-locking.html#LOCKING-ROWS).
type RowLock = Literal["UPDATE", "NO KEY UPDATE", "KEY SHARE"]
# SQLAlchemy's ``with_for_update`` arguments (read, key_share) for each.
_FOR_UPDATE: Final[Mapping[RowLock, tuple[bool, bool]]] = MappingProxyType(
    {"UPDATE": (False, False), "NO KEY UPDATE": (False, True), "KEY SHARE": (True, True)}
)


def lock_repositories(
    session: Session, repository_ids: Collection[int] | Select[int], *, lock: RowLock
) -> None:
    """Lock the stored repositories with these IDs in ID order, until the transaction ends.

    The lock order. Every path takes the rows it locks in this order, and only the steps it needs,
    so no two paths wait on each other in opposite orders (a deadlock):

    1. repositories, in ID order, through this function;
    2. accounts, in ID order, with their installations;
    3. the user row;
    4. the user's own rows (sessions, access lists, email address and choices), whose order
       among themselves the user row lock makes safe.

    Rows a path inserts follow the same order, in ID order. Without the ORDER BY here the order
    would be whatever the query plan yields; a bulk DELETE or UPDATE has no order of its own, so
    a path locks its rows with a separate statement first. Delete my data, a sign-in (a stale
    installation's uninstall included), ``store_job``, an uninstall, a removal, the
    reconciliation, a backfill run, the backfill queue and a re-queue, and the daily purge follow
    it. The queue and a re-queue also insert their queued rows in ID order, so the two at once
    never each wait for the other's new row: Postgres inserts an INSERT ... SELECT's rows in its
    ORDER BY order in practice, though it does not document it.
    """
    read, key_share = _FOR_UPDATE[lock]
    session.execute(
        select(Repository.id)
        .where(Repository.id.in_(repository_ids))
        .order_by(Repository.id)
        .with_for_update(read=read, key_share=key_share)
    ).all()


def delete_my_data(session: Session, user_id: int) -> Deleted:
    """Delete a user, their email address and choices, sessions, access lists and personal jobs.

    The personal account is the account with the user's own ID and type ``User``. Its repository
    rows are locked first, in ID order as a re-queue locks them, and before the queued row or the
    account, as a backfill and ``store_job`` lock them, so a run in progress finishes before the
    jobs go. The
    repositories leave the backfill queue and are marked backfilled, so the reconciliation does
    not queue them again; a lost event or a gap may re-queue them later. Then the account's
    ``data_deleted_at`` is set, so no path (a webhook, a redelivery or a backfill) stores a job
    created before it again, and no backfill starts before it. Jobs created later are new jobs,
    collected while the App stays installed. Organisation data stays: it belongs to the
    organisation and goes when the App is uninstalled.
    """
    personal = (
        select(Installation.id).join(Account).where(Account.id == user_id, Account.type == "User")
    )
    repositories = _repositories(personal)
    # Repository rows first, as a backfill run takes them, and in ID order, as a re-queue takes
    # them.
    lock_repositories(session, repositories, lock="UPDATE")
    # Then the account (the order store_job takes them): from now on no job created before this
    # moment is stored again, by any path.
    session.execute(
        update(Account)
        .where(Account.id == user_id, Account.type == "User")
        .values(data_deleted_at=func.now())
    )
    backfills = _count(session, delete(Backfill).where(Backfill.repository_id.in_(repositories)))
    session.execute(
        update(Repository).where(Repository.id.in_(repositories)).values(backfilled_at=func.now())
    )
    # Keyword arguments run in order, so children go before their parents.
    return Deleted(
        backfills=backfills,
        jobs=_count(session, delete(Job).where(Job.repository_id.in_(repositories))),
        **_user_rows(session, user_id),
    )


def revoke_authorization(session: Session, user_id: int) -> Deleted:
    """Delete a user who revoked the App's authorization: address, choices, sessions, lists.

    Revoking does not uninstall the App, so every account's job data stays; it goes on uninstall.
    A user who is not stored deletes nothing.
    """
    return Deleted(**_user_rows(session, user_id))


def uninstall(session: Session, installation_id: int) -> Deleted:
    """Delete an installation's account and everything stored under it.

    Users and their sessions stay; each user's lists lose the installation and its repositories.
    An installation that is not stored deletes nothing.
    """
    account_id = session.scalar(
        select(Installation.account_id).where(Installation.id == installation_id)
    )
    if account_id is None:
        return Deleted()
    installations = select(Installation.id).where(Installation.account_id == account_id)
    repositories = _repositories(installations)
    # In the lock order (``lock_repositories``): the repositories, then the account with its
    # installation, before any user's lists.
    lock_repositories(session, repositories, lock="UPDATE")
    session.execute(
        select(Account.id).join(Installation).where(Account.id == account_id).with_for_update()
    ).all()
    # Keyword arguments run in order, so children go before their parents. The installation
    # lists go before the repository lists, as a sign-in and Delete my data delete them.
    return Deleted(
        jobs=_count(session, delete(Job).where(Job.repository_id.in_(repositories))),
        user_installations=_count(
            session,
            delete(UserInstallation).where(UserInstallation.installation_id.in_(installations)),
        ),
        user_repositories=_user_repositories(session, repositories),
        repositories=_count(session, delete(Repository).where(Repository.id.in_(repositories))),
        label_sizes=_count(session, delete(LabelSize).where(LabelSize.account_id == account_id)),
        installations=_count(
            session, delete(Installation).where(Installation.account_id == account_id)
        ),
        accounts=_count(session, delete(Account).where(Account.id == account_id)),
    )


def remove_repositories(
    session: Session, installation_id: int, repository_ids: Collection[int]
) -> Deleted:
    """Delete repositories an installation can no longer access, and their jobs.

    Only that installation's own repositories go: an ID it does not hold deletes nothing.
    """
    repositories = select(Repository.id).where(
        Repository.installation_id == installation_id, Repository.id.in_(repository_ids)
    )
    # The repositories first, in the lock order (``lock_repositories``).
    lock_repositories(session, repositories, lock="UPDATE")
    # Keyword arguments run in order, so children go before their parents.
    return Deleted(
        jobs=_count(session, delete(Job).where(Job.repository_id.in_(repositories))),
        user_repositories=_user_repositories(session, repositories),
        repositories=_count(session, delete(Repository).where(Repository.id.in_(repositories))),
    )


def _user_rows(session: Session, user_id: int) -> dict[str, int]:
    # The user row before the user's own rows, in the lock order (``lock_repositories``), so a
    # sign-in that holds the user row is waited for. FOR NO KEY UPDATE, the lock a sign-in's upsert
    # takes, not FOR UPDATE: an email choice's save holds its choice's row and then takes FOR KEY
    # SHARE on the user row for its consent row; FOR UPDATE would block that while this waits for
    # the choice's row, a deadlock.
    session.execute(
        select(GithubUser.id).where(GithubUser.id == user_id).with_for_update(key_share=True)
    ).all()
    # A dict keeps insertion order, so children go before their parents here too.
    return {
        "sessions": _count(session, delete(UserSession).where(UserSession.user_id == user_id)),
        "user_installations": _count(
            session, delete(UserInstallation).where(UserInstallation.user_id == user_id)
        ),
        "user_repositories": _count(
            session, delete(UserRepository).where(UserRepository.user_id == user_id)
        ),
        "user_emails": _count(session, delete(UserEmail).where(UserEmail.user_id == user_id)),
        "email_preferences": _count(
            session, delete(EmailPreference).where(EmailPreference.user_id == user_id)
        ),
        "email_consents": _count(
            session, delete(EmailConsent).where(EmailConsent.user_id == user_id)
        ),
        "users": _count(session, delete(GithubUser).where(GithubUser.id == user_id)),
    }


def _user_repositories(session: Session, repositories: Select[int]) -> int:
    return _count(
        session, delete(UserRepository).where(UserRepository.repository_id.in_(repositories))
    )


def _repositories(installations: Select[int]) -> Select[int]:
    return select(Repository.id).where(Repository.installation_id.in_(installations))


def _count(session: Session, statement: Delete) -> int:
    # An ORM-enabled DELETE returns a CursorResult, which carries the row count; the session
    # flushes pending changes first and drops the deleted rows from its identity map.
    return cast(CursorResult[Any], session.execute(statement)).rowcount


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must carry a timezone")
    return now
