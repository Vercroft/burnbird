"""The hourly step that queues run-out warnings: the most severe new one per allowance and person.

It runs in the worker before the sending step, and only with the switch on
(``config.run_out_warnings_from_environment``). Each run is claimed once an hour
(``github.schedule``). No network call is made here.

**Who.** For each account with an installation that is not suspended, in account ID order: every
user the installation lists who may see the account's whole use (``account_access``), whose
address a sign-in read within ``retention.EMAIL_FRESHNESS`` and is not suppressed, and who has
run-out warnings on.

**What.** For each provider and only the kinds its ``email_warnings`` lists, the warnings
``usage.allowance_state`` makes true now. A warning is keyed once per kind, account, provider,
cycle and person (``outbox.run_out_warning_key``): one row per key, written with
``ON CONFLICT DO NOTHING``. Of the kinds that may go (true now, and either with no row yet or with
a row skipped for a reversible reason, ``REVERSIBLE``), only the most severe is queued, and only
if no more severe row blocks it; nothing is recorded for the others. The order, most severe
first, is ``SEVERITY``. A row blocks a less severe kind only while it is queued, sent or unknown
(``BLOCKING``); a skipped one never does. A row skipped as ``superseded`` or ``disabled`` comes
back, the same row put back to queued with today's top repository; every other skipped outcome
is final for the cycle, and a sent or unknown row never comes back. A queued less severe row is
finished as ``superseded`` when a more severe one is queued, so none is sent after it. A queued
row expires at the cycle's reset, and names the cycle's top repository and runner size by ID
only.

**Locks.** One transaction per account, in this order: the ``email-warnings`` task row FOR UPDATE,
so two runs queue one after the other; the account FOR KEY SHARE (level 2 of
``retention.lock_repositories``); the recipients' user rows FOR SHARE in ID order (level 3), the
lock Delete my data's own user lock (FOR NO KEY UPDATE) waits for, so it never holds a person's
outbox rows while this waits for them; then the outbox rows (level 4): queued ones locked in ID
order before they are superseded, skipping one the sender holds right now, so a run never waits
for a send; the row put back; and the inserts, each ``ON CONFLICT (dedupe_key) DO NOTHING``.
Jobs are read without a lock, and no repository is locked.
"""

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.dependencies import account_access
from burnbird.billing import usage
from burnbird.billing.forecast import WarningKind
from burnbird.billing.providers import Provider, load_table
from burnbird.billing.usage import AllowanceState, Usage
from burnbird.email import outbox
from burnbird.email.run_out import BLOCKING, SEVERITY, TEMPLATE, WarningParams
from burnbird.github import schedule
from burnbird.models import (
    Account,
    EmailOutbox,
    EmailPreference,
    GithubUser,
    Installation,
    UserEmail,
    UserInstallation,
    WorkerTask,
)
from burnbird.retention import EMAIL_FRESHNESS

logger = logging.getLogger(__name__)

TASK: Final = "email-warnings"
INTERVAL: Final = timedelta(hours=1)
# A row skipped for one of these reasons comes back when its kind may go again: overtaken by a
# more severe warning that was then not sent, or stopped by the switch. Every other skipped
# outcome is final for the cycle, and a sent or unknown row never comes back.
REVERSIBLE: Final = frozenset({"superseded", "disabled"})
_REVERSIBLE: Final = frozenset(("skipped", outcome) for outcome in REVERSIBLE)


def run(sessions: sessionmaker[Session], now: datetime) -> bool:
    """Queue the run-out warnings due now, if this hour's run is not claimed yet; return if run.

    One transaction per account, so a run stopped halfway keeps what it queued, and the next run
    queues nothing twice.
    """
    claimed = schedule.claim(sessions, TASK, now, INTERVAL)
    if claimed is None:
        return False
    with sessions.begin() as session:
        accounts = session.scalars(
            select(Account.id)
            .join(Installation, Installation.account_id == Account.id)
            .where(Installation.suspended.is_(False))
            .order_by(Account.id)
        ).all()
    complete = True
    for account_id in accounts:
        try:
            queue_account(sessions, account_id, now)
        except Exception:
            # One account's failure stops neither the others nor the next run, which tries it
            # again; what was queued stays queued.
            logger.exception("email warnings: account %d failed", account_id)
            complete = False
    schedule.finish(sessions, claimed, complete=complete)
    return True


def queue_account(sessions: sessionmaker[Session], account_id: int, now: datetime) -> None:
    """Queue one account's run-out warnings due now, in one transaction."""
    with sessions.begin() as session:
        _lock_task(session, now)
        installation_id = session.scalar(
            select(Installation.id)
            .join(Account, Account.id == Installation.account_id)
            .where(Account.id == account_id, Installation.suspended.is_(False))
            .with_for_update(key_share=True, read=True, of=Account)
        )
        if installation_id is None:
            return
        recipients = _recipients(session, installation_id, now)
        if not recipients:
            return
        for provider in load_table().providers:
            if not provider.email_warnings:
                continue
            state = usage.allowance_state(session, account_id, provider.id, now)
            if state is None:
                continue
            kinds = {w.kind for w in state.forecast.warnings} & set(provider.email_warnings)
            if not kinds:
                continue
            top = usage.top_usage(
                session,
                installation_id,
                provider.id,
                state.forecast.start,
                now.astimezone(UTC).date() + timedelta(days=1),
            )
            first = top[0] if top else None
            for user_id in recipients:
                _queue_most_severe(
                    session, account_id, user_id, provider, state, kinds, first, now
                )


def _lock_task(session: Session, now: datetime) -> None:
    session.execute(
        insert(WorkerTask)
        .values(name=TASK, next_run_at=now)
        .on_conflict_do_nothing(index_elements=[WorkerTask.name])
    )
    session.execute(select(WorkerTask.name).where(WorkerTask.name == TASK).with_for_update())


def _recipients(session: Session, installation_id: int, now: datetime) -> list[int]:
    """Return, locked in ID order, the users who get the account's warnings."""
    listed = session.scalars(
        select(UserInstallation.user_id)
        .join(UserEmail, UserEmail.user_id == UserInstallation.user_id)
        .join(EmailPreference, EmailPreference.user_id == UserInstallation.user_id)
        .where(
            UserInstallation.installation_id == installation_id,
            # The purge's own rule (``retention``): an address read longer ago is deleted.
            UserEmail.read_at >= now - EMAIL_FRESHNESS,
            UserEmail.suppressed_at.is_(None),
            EmailPreference.kind == "usage_warnings",
            EmailPreference.enabled.is_(True),
        )
        .order_by(UserInstallation.user_id)
    ).all()
    if not listed:
        return []
    # FOR SHARE, not FOR KEY SHARE: it conflicts with the FOR NO KEY UPDATE that Delete my data
    # takes on the user row before it deletes the user's outbox rows, so Delete my data waits here
    # and never holds a row this transaction then waits for.
    locked = session.scalars(
        select(GithubUser.id)
        .where(GithubUser.id.in_(listed))
        .order_by(GithubUser.id)
        .with_for_update(read=True)
    ).all()
    return [user_id for user_id in locked if account_access(session, user_id, installation_id)]


def _queue_most_severe(
    session: Session,
    account_id: int,
    user_id: int,
    provider: Provider,
    state: AllowanceState,
    kinds: set[WarningKind],
    top: Usage | None,
    now: datetime,
) -> None:
    keys = {
        kind: outbox.run_out_warning_key(
            kind.value, account_id, provider.id, state.forecast.start, user_id
        )
        for kind in SEVERITY
    }
    recorded = {
        key: (row_state, outcome)
        for key, row_state, outcome in session.execute(
            select(EmailOutbox.dedupe_key, EmailOutbox.state, EmailOutbox.outcome).where(
                EmailOutbox.dedupe_key.in_(keys.values())
            )
        )
    }
    # A kind may go if it is true now and its key is free, or its row was skipped for a
    # reversible reason; only the most severe of them, and only if nothing more severe blocks it.
    eligible = [
        kind
        for kind in SEVERITY
        if kind in kinds and (keys[kind] not in recorded or recorded[keys[kind]] in _REVERSIBLE)
    ]
    if not eligible:
        return
    most = eligible[0]
    if any(
        recorded.get(keys[kind], ("", None))[0] in BLOCKING
        for kind in SEVERITY[: SEVERITY.index(most)]
    ):
        return
    expires_at = datetime.combine(state.forecast.reset, time(), UTC)
    # Today's top repository and runner size, as IDs, for a new row and a row put back alike.
    params = WarningParams(
        provider.id,
        most,
        state.forecast.start,
        None if top is None else top.repository_id,
        None if top is None else top.runner_class,
    ).to_params()
    # A queued less severe warning is superseded, locked in ID order first. One the sender holds
    # right now is left alone, never waited for: it is already going out, and a less severe
    # warning before a more severe one is allowed.
    less = [keys[kind] for kind in SEVERITY[SEVERITY.index(most) + 1 :]]
    superseded = session.scalars(
        select(EmailOutbox.id)
        .where(EmailOutbox.dedupe_key.in_(less), EmailOutbox.state == "queued")
        .order_by(EmailOutbox.id)
        .with_for_update(skip_locked=True)
    ).all()
    if superseded:
        session.execute(
            update(EmailOutbox)
            .where(EmailOutbox.id.in_(superseded))
            .values(state="skipped", outcome="superseded", finished_at=now, retry_at=None)
        )
    if keys[most] in recorded:
        # The same row comes back, so there is still one row per key.
        session.execute(
            update(EmailOutbox)
            .where(
                EmailOutbox.dedupe_key == keys[most],
                EmailOutbox.state == "skipped",
                EmailOutbox.outcome.in_(REVERSIBLE),
            )
            .values(
                state="queued",
                outcome=None,
                finished_at=None,
                attempts=0,
                retry_at=now,
                expires_at=expires_at,
                params=params,
            )
        )
        return
    outbox.enqueue(
        session,
        user_id=user_id,
        account_id=account_id,
        kind="usage_warnings",
        template=TEMPLATE,
        dedupe_key=keys[most],
        params=params,
        now=now,
        expires_at=expires_at,
    )
