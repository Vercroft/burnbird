"""What is deleted, and when: retention past its limit, "Delete my data", and an uninstall.

Pure functions over a Session. They never commit: the caller runs each in one transaction, so a
failure deletes nothing. Children are deleted before their parents, so every count is exact; the
tables' ON DELETE CASCADE keys are only the safety net.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import CursorResult, Delete, Select, delete, select
from sqlalchemy.orm import Session

from burnbird.models import (
    Account,
    GithubUser,
    Installation,
    Job,
    LabelSize,
    Repository,
    UserInstallation,
    UserSession,
    WebhookDelivery,
)

JOB_RETENTION: Final = timedelta(days=90)
DELIVERY_RETENTION: Final = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class Deleted:
    """How many rows of each table one deletion removed."""

    jobs: int = 0
    repositories: int = 0
    label_sizes: int = 0
    user_installations: int = 0
    installations: int = 0
    accounts: int = 0
    sessions: int = 0
    users: int = 0


def purge_jobs(session: Session, now: datetime) -> int:
    """Delete the jobs created more than 90 days before ``now``; return how many."""
    cutoff = _aware(now) - JOB_RETENTION
    return _count(session, delete(Job).where(Job.created_at < cutoff))


def purge_deliveries(session: Session, now: datetime) -> int:
    """Delete the webhook deliveries received more than 7 days before ``now``; return how many."""
    cutoff = _aware(now) - DELIVERY_RETENTION
    return _count(session, delete(WebhookDelivery).where(WebhookDelivery.received_at < cutoff))


def purge_sessions(session: Session, now: datetime) -> int:
    """Delete the sessions that have expired at ``now``; return how many."""
    return _count(session, delete(UserSession).where(UserSession.expires_at <= _aware(now)))


def delete_my_data(session: Session, user_id: int) -> Deleted:
    """Delete a user, their sessions and installation list, and their personal account's jobs.

    The personal account is the account with the user's own ID and type ``User``. Organisation
    data stays: it belongs to the organisation and goes when the App is uninstalled.
    """
    personal = (
        select(Installation.id).join(Account).where(Account.id == user_id, Account.type == "User")
    )
    # Keyword arguments run in order, so children go before their parents.
    return Deleted(
        jobs=_count(session, delete(Job).where(Job.repository_id.in_(_repositories(personal)))),
        sessions=_count(session, delete(UserSession).where(UserSession.user_id == user_id)),
        user_installations=_count(
            session, delete(UserInstallation).where(UserInstallation.user_id == user_id)
        ),
        users=_count(session, delete(GithubUser).where(GithubUser.id == user_id)),
    )


def uninstall(session: Session, installation_id: int) -> Deleted:
    """Delete an installation's account and everything stored under it.

    Users and their sessions stay; each user's list loses the installation. An installation that
    is not stored deletes nothing.
    """
    account_id = session.scalar(
        select(Installation.account_id).where(Installation.id == installation_id)
    )
    if account_id is None:
        return Deleted()
    installations = select(Installation.id).where(Installation.account_id == account_id)
    repositories = _repositories(installations)
    # Keyword arguments run in order, so children go before their parents.
    return Deleted(
        jobs=_count(session, delete(Job).where(Job.repository_id.in_(repositories))),
        repositories=_count(session, delete(Repository).where(Repository.id.in_(repositories))),
        user_installations=_count(
            session,
            delete(UserInstallation).where(UserInstallation.installation_id.in_(installations)),
        ),
        label_sizes=_count(session, delete(LabelSize).where(LabelSize.account_id == account_id)),
        installations=_count(
            session, delete(Installation).where(Installation.account_id == account_id)
        ),
        accounts=_count(session, delete(Account).where(Account.id == account_id)),
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
