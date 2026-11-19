"""The email outbox: an email is queued once, then sent by the worker one at a time, at most once.

**Queue.** ``enqueue`` writes a row in the caller's transaction, the one that decides to send it,
so an email exists only if that decision commits. A second decision for the same email (the same
``dedupe_key``) is a no-op.

**Send** (the worker's ``email-send`` step, ``run``):

1. **Lease.** ``claim`` takes the oldest due row with ``FOR UPDATE SKIP LOCKED``, moves its
   ``retry_at`` ``LEASE`` ahead and counts the attempt, and commits: two workers never take the
   same row, and a worker that dies leaves it due again after the lease (the backfill's pattern).
2. **Send transaction.** ``send`` locks the ``email-send`` task row, so sends are one at a time
   across workers and the caps cannot be raced, then the outbox row, and goes on only while the
   lease is still its own. The address is read without a lock. It checks, in order: expiry, the
   address, its freshness (one a sign-in has not read within ``retention.EMAIL_FRESHNESS`` is
   ``stale_address``, as the daily purge would delete it), its suppression, the user's choice
   (usage email only), the user's access to the account, then the per-recipient and global caps,
   and only then a run-out warning's state (``burnbird.email.run_out``), so a row that waits at a
   cap reads no jobs. A row that fails a check is ``skipped`` with the reason; one at a cap waits,
   queued, for the window to free; one whose params are not a run-out warning's fails. Otherwise
   it renders the email, naming a run-out warning's top repository only if GitHub said, before
   the transaction began and with no lock held, that the recipient can see it
   and makes the one POST while the outbox row stays locked: Delete my data, a revoke or an
   uninstall, which delete outbox rows first, wait for it, and nothing is sent after they commit.
   The sender's timeouts bound that wait.
3. **Outcome.** ``sent``, ``unknown`` and ``failed`` are final. An unknown outcome (the provider
   may have taken it) is never sent again: a missed warning is better than a double one, and the
   page still shows it. ``retry`` comes back after a growing wait and fails after
   ``MAX_ATTEMPTS``; the provider's daily limit pauses every send until its reset; a configuration
   error pauses every send for ``CONFIG_PAUSE``. A sender that raises counts as unknown, and an
   email that cannot be rendered fails, so neither is tried again. Two duplicates are left, both
   after the POST and before the outcome commits: a worker killed during the POST, or a commit that
   fails (the database restarts or the connection drops). The transaction rolls back and the row is
   sent again after its lease; no code can tell that the POST was made.

**Caps** are rolling 24-hour counts of sent and unknown emails, set well below the provider's daily
limits. That margin is what keeps any calendar day of the provider under its limits: deleting a
user's data deletes their rows, so the rolling count can be lower than the provider's own count.
The log names the outbox row, the user and an outcome code, never an address.
"""

import hashlib
import logging
import secrets
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from typing import Any, Final

from jinja2 import Environment
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.dependencies import account_access
from burnbird.email import run_out
from burnbird.email.message import compose, unsubscribe_url
from burnbird.email.sender import Result, Sender
from burnbird.github.client import GithubApp
from burnbird.models import EmailOutbox, EmailPreference, Installation, UserEmail, WorkerTask
from burnbird.retention import EMAIL_FRESHNESS
from burnbird.templates import ENVIRONMENT

logger = logging.getLogger(__name__)

TASK: Final = "email-send"
LEASE: Final = timedelta(minutes=5)
# One pass sends at most this many, so a backlog never keeps a pass running, and a pass of slow
# sends (each bounded by the sender's timeouts, about 20 s) ends before its last claim's lease.
EMAILS_PER_PASS: Final = 10
# Below the provider's daily limits.
GLOBAL_CAP: int = 80
RECIPIENT_CAP: int = 10
CAP_WINDOW: Final = timedelta(hours=24)
MAX_ATTEMPTS: Final = 6
BACKOFF_BASE: Final = timedelta(seconds=60)
BACKOFF_CAP: Final = timedelta(hours=1)
CONFIG_PAUSE: Final = timedelta(hours=1)
# The provider resets its daily limits at "12AM (server time)" without naming the zone; read as
# 00:00 India time, 18:30 UTC (inferred, not stated).
PROVIDER_RESET_UTC: Final = clock_time(18, 30)
# How often a cap reached is logged as an ERROR, at most.
CAP_ERROR_EVERY_SECONDS: Final = 3600.0
# Counted towards the caps: everything that may have reached the recipient.
_COUNTED: Final = ("sent", "unknown")


@dataclass(frozen=True, slots=True)
class Mail:
    """What sending needs: the sender, the site's origin for links, the templates and GitHub.

    ``github`` asks whether a recipient can see a run-out warning's top repository; without it
    the repository is never named. ``run_out_warnings`` is the switch
    (``config.run_out_warnings_from_environment``): off, a queued run-out warning is skipped as
    ``disabled``.
    """

    sender: Sender
    origin: str
    environment: Environment = field(default=ENVIRONMENT)
    github: GithubApp | None = None
    run_out_warnings: bool = False


@dataclass(frozen=True, slots=True)
class Claimed:
    """A leased row: its ID, when the lease ends, and the attempts counted so far."""

    id: uuid.UUID
    leased_until: datetime
    attempts: int


def enqueue(
    session: Session,
    *,
    user_id: int,
    account_id: int | None,
    kind: str,
    template: str,
    dedupe_key: str,
    params: Mapping[str, Any],
    now: datetime,
    expires_at: datetime,
) -> bool:
    """Queue an email in the caller's transaction; return False if it was queued before.

    ``params`` holds IDs, kinds and dates for the template, never an address or a login.
    """
    queued = session.execute(
        insert(EmailOutbox)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            account_id=account_id,
            kind=kind,
            template=template,
            dedupe_key=dedupe_key,
            params=dict(params),
            created_at=now,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=[EmailOutbox.dedupe_key])
        .returning(EmailOutbox.id)
    ).first()
    return queued is not None


def run(sessions: sessionmaker[Session], mail: Mail, now: datetime) -> int:
    """Send due emails, at most ``EMAILS_PER_PASS``; return how many were taken."""
    if now.utcoffset() is None:
        # A naive time never equals a lease read back from the database, so nothing would be sent.
        raise ValueError("the email outbox needs a time with a timezone")
    taken = 0
    while taken < EMAILS_PER_PASS and not _paused(sessions, now):
        claimed = claim(sessions, now)
        if claimed is None:
            break
        send(sessions, mail, claimed, now)
        taken += 1
    return taken


def claim(sessions: sessionmaker[Session], now: datetime) -> Claimed | None:
    """Lease the oldest due row for ``LEASE`` and count the attempt; None when none is free."""
    leased_until = now + LEASE
    with sessions.begin() as session:
        due = (
            select(EmailOutbox.id)
            .where(
                EmailOutbox.state == "queued",
                or_(EmailOutbox.retry_at.is_(None), EmailOutbox.retry_at <= now),
            )
            .order_by(EmailOutbox.retry_at.nulls_first(), EmailOutbox.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        row = session.execute(
            update(EmailOutbox)
            .where(EmailOutbox.id == due)
            .values(retry_at=leased_until, attempts=EmailOutbox.attempts + 1)
            .returning(EmailOutbox.id, EmailOutbox.attempts)
        ).first()
    return None if row is None else Claimed(row[0], leased_until, row[1])


def send(sessions: sessionmaker[Session], mail: Mail, claimed: Claimed, now: datetime) -> None:
    """Check the leased row, then send it once or say why not, in one transaction.

    A run-out warning's repository check is a network call, so it is made first, with no
    transaction open: the send transaction holds its locks only for the database and the POST.
    """
    answer = _ask_about_repository(sessions, mail, claimed, now)
    with sessions.begin() as session:
        task = _lock_task(session, now)
        row = session.scalars(
            select(EmailOutbox).where(EmailOutbox.id == claimed.id).with_for_update()
        ).first()
        if row is None or row.state != "queued" or row.retry_at != claimed.leased_until:
            # Deleted with its user, or the lease ran out and another worker took it over.
            return
        if task.next_run_at > now:
            _put_back(row, now)
            return
        if row.template == run_out.TEMPLATE and not mail.run_out_warnings:
            # The switch is off: a warning already queued is not sent either.
            _skip(row, "disabled", now)
            return
        try:
            address, read_at = _checked_address(session, row, now)
        except _SkippedError as skip:
            _skip(row, skip.reason, now)
            return
        if wait := _cap_wait(session, row, now):
            _put_back(row, wait)
            _CAP_ERRORS.log(session)
            return
        warning = None
        if row.template == run_out.TEMPLATE:
            try:
                warning = run_out.checked(session, row, now)
            except run_out.SkippedError as skip:
                _skip(row, skip.reason, now)
                return
            except run_out.InvalidParamsError:
                # Wrong params stay wrong: fail the email rather than try it every lease.
                logger.exception("email: %s has params it cannot be sent with", row.id)
                _finish(row, "failed", "params", now)
                return
        token = secrets.token_urlsafe(32) if row.kind == "usage_warnings" else None
        try:
            context = _context(session, row, warning, answer, read_at, token, now)
            message = _render(mail, row, address, token, context)
        except Exception:
            # A broken template stays broken: fail the email rather than try it every lease.
            logger.exception("email: %s could not be rendered, user %d", row.id, row.user_id)
            _finish(row, "failed", "render", now)
            return
        try:
            result = mail.sender.send(message)
        except Exception:
            # The sender is meant never to raise. If it does, the POST may have been made, and a
            # rollback would send the email again after the lease.
            logger.exception("email: %s: the sender raised, user %d", row.id, row.user_id)
            result = Result("unknown", "error")
        _record(row, task, result, token, now)


def _ask_about_repository(
    sessions: sessionmaker[Session], mail: Mail, claimed: Claimed, now: datetime
) -> run_out.RepositoryAnswer | None:
    """Ask GitHub, with no transaction open, whether the recipient can see the top repository.

    The question is read in a short transaction that locks nothing, and only for a row that may
    go now: still this lease's, past the address checks and under the caps. A row that will be
    skipped or waits costs no call; the send transaction still decides. The call is made as every
    GET is, so at worst a token request and three tries with their waits, about 45 seconds, well
    inside the lease.
    """
    if not mail.run_out_warnings:
        return None
    with sessions.begin() as session:
        row = session.get(EmailOutbox, claimed.id)
        if row is None or row.state != "queued" or row.retry_at != claimed.leased_until:
            return None
        question = run_out.repository_question(session, row)
        if question is None:
            return None
        try:
            _checked_address(session, row, now)
        except _SkippedError:
            return None
        if _cap_wait(session, row, now) is not None:
            return None
    return run_out.ask(mail.github, claimed.id, question)


def _paused(sessions: sessionmaker[Session], now: datetime) -> bool:
    with sessions.begin() as session:
        paused_until = session.scalar(
            select(WorkerTask.next_run_at).where(WorkerTask.name == TASK)
        )
    return paused_until is not None and paused_until > now


def _lock_task(session: Session, now: datetime) -> WorkerTask:
    session.execute(
        insert(WorkerTask)
        .values(name=TASK, next_run_at=now)
        .on_conflict_do_nothing(index_elements=[WorkerTask.name])
    )
    return session.scalars(
        select(WorkerTask).where(WorkerTask.name == TASK).with_for_update()
    ).one()


class _SkippedError(Exception):
    """The row is not sent, for ``reason``: an outcome code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _checked_address(session: Session, row: EmailOutbox, now: datetime) -> tuple[str, datetime]:
    """Return the address to send the row to and when a sign-in last read it.

    Raise ``_SkippedError`` saying why not.
    """
    if now >= row.expires_at:
        raise _SkippedError("expired")
    email = session.execute(
        select(UserEmail.address, UserEmail.read_at, UserEmail.suppressed_at).where(
            UserEmail.user_id == row.user_id
        )
    ).first()
    if email is None:
        raise _SkippedError("no_address")
    # The daily purge's own rule (``retention``): until it runs, the address is not mailed.
    if email.read_at < now - EMAIL_FRESHNESS:
        raise _SkippedError("stale_address")
    if email.suppressed_at is not None:
        raise _SkippedError("suppressed")
    if row.kind == "usage_warnings" and not session.scalar(
        select(EmailPreference.enabled).where(
            EmailPreference.user_id == row.user_id, EmailPreference.kind == "usage_warnings"
        )
    ):
        raise _SkippedError("off")
    if row.account_id is not None:
        installation = session.scalar(
            select(Installation.id).where(Installation.account_id == row.account_id)
        )
        if installation is None or not account_access(session, row.user_id, installation):
            raise _SkippedError("no_access")
    address: str = email.address
    read_at: datetime = email.read_at
    return address, read_at


def _cap_wait(session: Session, row: EmailOutbox, now: datetime) -> datetime | None:
    """Return when the row may go if a cap is reached now, or None when it may go now."""
    window = select(func.count(), func.min(EmailOutbox.finished_at)).where(
        EmailOutbox.state.in_(_COUNTED), EmailOutbox.finished_at > now - CAP_WINDOW
    )
    waits = []
    for cap, counted in (
        (GLOBAL_CAP, window),
        (RECIPIENT_CAP, window.where(EmailOutbox.user_id == row.user_id)),
    ):
        sent, oldest = session.execute(counted).one()
        if sent >= cap and oldest is not None:
            # Sends are one at a time, so the count never passes the cap: the oldest one leaving
            # the window frees a place.
            waits.append(oldest + CAP_WINDOW)
    return max(waits, default=None)


def _skip(row: EmailOutbox, reason: str, now: datetime) -> None:
    _finish(row, "skipped", reason, now)
    logger.info("email: %s skipped (%s), user %d", row.id, reason, row.user_id)


def _context(
    session: Session,
    row: EmailOutbox,
    warning: run_out.Checked | None,
    answer: run_out.RepositoryAnswer | None,
    read_at: datetime,
    token: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Return the values the email's templates use, read at send."""
    context: dict[str, Any] = dict(row.params)
    if warning is not None and row.account_id is not None:
        context = run_out.context(
            warning,
            account=run_out.account_login(session, row.account_id),
            repository=None if answer is None else answer.name_for(row, warning),
            now=now,
        )
    if token is not None:
        # The day the address is deleted unless a sign-in reads it again.
        context["until"] = run_out.utc_date((read_at + EMAIL_FRESHNESS).astimezone(UTC).date())
    return context


def _render(
    mail: Mail, row: EmailOutbox, address: str, token: str | None, context: Mapping[str, Any]
) -> Any:
    environment = mail.environment
    subject = environment.get_template(f"email/{row.template}.subject.txt").render(context)
    return compose(
        reference=row.id,
        to=address,
        # One line: a value holding a line break could otherwise start a header of its own.
        subject=" ".join(subject.split()),
        html=environment.get_template(f"email/{row.template}.html"),
        text=environment.get_template(f"email/{row.template}.txt"),
        context=context,
        origin=mail.origin,
        unsubscribe_url=None if token is None else unsubscribe_url(mail.origin, token),
    )


def _record(
    row: EmailOutbox, task: WorkerTask, result: Result, token: str | None, now: datetime
) -> None:
    status, code = result.status, result.code
    if status in ("sent", "unknown"):
        row.provider_request_id = result.request_id
        # An unknown email may have arrived, so its link works as a sent one's does.
        if token is not None:
            row.unsubscribe_sha256 = hashlib.sha256(token.encode()).digest()
        _finish(row, status, code, now)
    elif status == "retry" and row.attempts < MAX_ATTEMPTS:
        row.outcome = code
        row.retry_at = now + min(BACKOFF_BASE * 2**row.attempts, BACKOFF_CAP)
    elif status == "reset":
        _put_back(row, _next_reset(now))
        row.outcome = code
        # Every send waits: the provider refuses them all until then.
        task.next_run_at = _next_reset(now)
        logger.error(
            "email: the provider's daily limit is reached (%s); next send after its reset", code
        )
    elif status == "config":
        _put_back(row, now)
        row.outcome = code
        task.next_run_at = now + CONFIG_PAUSE
        logger.error(
            "email: the provider refused our configuration (%s); sending paused an hour", code
        )
    else:
        # Failed, or retried too often.
        _finish(row, "failed", code, now)
    logger.info("email: %s %s (%s), user %d", row.id, row.state, code, row.user_id)


def _finish(row: EmailOutbox, state: str, outcome: str, now: datetime) -> None:
    row.state = state
    row.outcome = outcome
    row.finished_at = now
    row.retry_at = None


def _put_back(row: EmailOutbox, retry_at: datetime) -> None:
    """Queue the row again for ``retry_at`` without counting the attempt: it was never made."""
    row.retry_at = retry_at
    row.attempts -= 1


def _next_reset(now: datetime) -> datetime:
    today = datetime.combine(now.astimezone(UTC).date(), PROVIDER_RESET_UTC, UTC)
    return today if today > now else today + timedelta(days=1)


class _CapErrors:
    """Logs a reached cap as an ERROR at most once an hour per worker process."""

    def __init__(self) -> None:
        self._last: float | None = None

    def log(self, session: Session) -> None:
        """Log the ERROR, unless one was logged within the hour."""
        moment = time.monotonic()
        if self._last is not None and moment - self._last < CAP_ERROR_EVERY_SECONDS:
            return
        self._last = moment
        queued = session.scalar(
            select(func.count()).select_from(EmailOutbox).where(EmailOutbox.state == "queued")
        )
        logger.error("email: a sending cap is reached; %d queued", queued)


_CAP_ERRORS: Final = _CapErrors()
