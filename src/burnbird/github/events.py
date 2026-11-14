"""The worker's GitHub step: turn stored webhook deliveries into jobs, installations and deletions.

One transaction per delivery. A delivery is claimed with ``FOR UPDATE SKIP LOCKED``, so two
workers never take the same one; PostgreSQL: SKIP LOCKED "can be used to avoid lock contention with
multiple consumers accessing a queue-like table"
(https://www.postgresql.org/docs/18/sql-select.html#SQL-FOR-UPDATE-SHARE). The delivery's own
work runs in a savepoint: if it fails, nothing it wrote stays, the failure is logged, and the
delivery is tried again later with a doubling wait, up to ``MAX_ATTEMPTS`` times. Every write is an
upsert or a keyed delete, so a repeat is safe.
"""

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.billing import counting
from burnbird.billing.classify import classify
from burnbird.github import schemas
from burnbird.models import Account, Installation, Job, LabelSize, Repository, WebhookDelivery

logger = logging.getLogger(__name__)

MAX_ATTEMPTS: Final = 5


class UnknownInstallationError(Exception):
    """A job arrived for an installation that is not stored, or not yet."""


def claim(session: Session, now: datetime) -> WebhookDelivery | None:
    """Lock and return the oldest delivery due at ``now``; None when none is due or free."""
    return session.scalars(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.processed_at.is_(None),
            WebhookDelivery.attempts < MAX_ATTEMPTS,
            or_(WebhookDelivery.retry_at.is_(None), WebhookDelivery.retry_at <= now),
        )
        .order_by(WebhookDelivery.received_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()


def process_next(sessions: sessionmaker[Session], now: datetime) -> bool:
    """Process the oldest due delivery in its own transaction; False when none is due."""
    with sessions.begin() as session:
        delivery = claim(session, now)
        if delivery is None:
            return False
        try:
            with session.begin_nested():
                HANDLERS[(delivery.event, delivery.action)](session, delivery.payload)
        except Exception:
            delivery.attempts += 1
            delivery.retry_at = now + timedelta(minutes=2**delivery.attempts)
            outcome = "gave up" if delivery.attempts >= MAX_ATTEMPTS else "will retry"
            logger.exception(
                "delivery %s (%s %s) failed on attempt %d; %s",
                delivery.id,
                delivery.event,
                delivery.action,
                delivery.attempts,
                outcome,
            )
        else:
            delivery.processed_at = now
    return True


def process_pending(sessions: sessionmaker[Session], now: datetime, limit: int) -> int:
    """Process due deliveries, at most ``limit``; return how many were taken."""
    taken = 0
    while taken < limit and process_next(sessions, now):
        taken += 1
    return taken


def _job_completed(session: Session, data: dict[str, Any]) -> None:
    event = schemas.WorkflowJobCompleted.model_validate(data)
    installation = session.get(Installation, event.installation.id)
    if installation is None:
        raise UnknownInstallationError(f"installation {event.installation.id} is not stored")
    repository = event.repository
    store_repository(session, installation.id, repository)
    rows = session.execute(
        select(LabelSize.label, LabelSize.size).where(
            LabelSize.account_id == installation.account_id
        )
    )
    label_sizes = {label: size for label, size in rows}
    job = event.workflow_job
    classification = classify(job.labels, job.runner_group_name, repository.private, label_sizes)
    billed = counting.bill(
        counting.Job(job.runner_id, job.started_at, job.completed_at), classification
    )
    job_values = {
        "run_id": job.run_id,
        "repository_id": repository.id,
        "workflow_name": job.workflow_name,
        "labels": list(job.labels),
        "runner_group_name": job.runner_group_name,
        "runner_name": job.runner_name,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "conclusion": job.conclusion,
        "provider": classification.provider,
        "runner_class": classification.runner.id if classification.runner is not None else None,
        "billed_minutes": billed.minutes if billed is not None else None,
        "amount": billed.amount if billed is not None else None,
    }
    # Keyed on the attempt too: a redelivery rewrites its row, and a re-run attempt is a new row.
    session.execute(
        insert(Job)
        .values(id=job.id, run_attempt=job.run_attempt, **job_values)
        .on_conflict_do_update(index_elements=[Job.id, Job.run_attempt], set_=job_values)
    )


def store_repository(
    session: Session, installation_id: int, repository: schemas.Repository
) -> None:
    """Store a repository under its installation, or update its name, privacy and installation.

    A job's delivery, a sign-in's list and the hourly reconciliation all use it.
    """
    values = {
        "installation_id": installation_id,
        "name": repository.name,
        "private": repository.private,
    }
    session.execute(
        insert(Repository)
        .values(id=repository.id, **values)
        .on_conflict_do_update(index_elements=[Repository.id], set_=values)
    )


def store_installation(session: Session, installation: schemas.Installation) -> None:
    """Store an installation and its account; a repeat leaves a stored one as it is.

    The webhook's ``installation.created`` and a sign-in's list of installations both use it.
    """
    account = installation.account
    # An account holds one installation. Another one stored for it means its "deleted" delivery
    # was lost, so it is uninstalled the same way first.
    stale = session.scalar(
        select(Installation.id).where(
            Installation.account_id == account.id, Installation.id != installation.id
        )
    )
    if stale is not None:
        retention.uninstall(session, stale)
    # The plan is the user's choice: it is set only when the account is new.
    session.execute(
        insert(Account)
        .values(
            id=account.id,
            login=account.login,
            type=account.type,
            github_plan=schemas.DEFAULT_PLAN[account.type],
        )
        .on_conflict_do_update(
            index_elements=[Account.id], set_={"login": account.login, "type": account.type}
        )
    )
    # A repeated "created" leaves a stored installation as it is, suspension included.
    session.execute(
        insert(Installation)
        .values(
            id=installation.id,
            account_id=account.id,
            created_at=installation.created_at,
            suspended=installation.suspended_at is not None,
        )
        .on_conflict_do_nothing(index_elements=[Installation.id])
    )


def _installation_created(session: Session, data: dict[str, Any]) -> None:
    store_installation(session, schemas.InstallationCreated.model_validate(data).installation)


def _installation_deleted(session: Session, data: dict[str, Any]) -> None:
    retention.uninstall(session, schemas.InstallationChanged.model_validate(data).installation.id)


def _set_suspended(suspended: bool) -> Callable[[Session, dict[str, Any]], None]:
    def handle(session: Session, data: dict[str, Any]) -> None:
        installation_id = schemas.InstallationChanged.model_validate(data).installation.id
        # An installation that is not stored has nothing to change.
        session.execute(
            update(Installation)
            .where(Installation.id == installation_id)
            .values(suspended=suspended)
        )

    return handle


def _repositories_removed(session: Session, data: dict[str, Any]) -> None:
    event = schemas.RepositoriesRemoved.model_validate(data)
    retention.remove_repositories(
        session,
        event.installation.id,
        [repository.id for repository in event.repositories_removed],
    )


def _authorization_revoked(session: Session, data: dict[str, Any]) -> None:
    retention.revoke_authorization(
        session, schemas.AuthorizationRevoked.model_validate(data).sender.id
    )


# One handler per pair in schemas.HANDLED; a test keeps the two in step.
HANDLERS: Final[Mapping[tuple[str, str], Callable[[Session, dict[str, Any]], None]]] = (
    MappingProxyType(
        {
            ("workflow_job", "completed"): _job_completed,
            ("installation", "created"): _installation_created,
            ("installation", "deleted"): _installation_deleted,
            ("installation", "suspend"): _set_suspended(True),
            ("installation", "unsuspend"): _set_suspended(False),
            ("installation_repositories", "removed"): _repositories_removed,
            ("github_app_authorization", "revoked"): _authorization_revoked,
        }
    )
)
