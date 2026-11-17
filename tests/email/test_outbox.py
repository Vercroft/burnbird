"""The outbox: an email is queued once, leased, checked, sent at most once, and capped.

Every test enqueues its own rows; the provider is a recording fake, so no email leaves.
"""

import hashlib
import logging
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Connection, event, select
from sqlalchemy.orm import Session, sessionmaker

from burnbird.email import outbox
from burnbird.email.outbox import Mail
from burnbird.email.sender import Result
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
from tests.email.examples import RecordingSender, example_environment

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
ORIGIN = "https://burnbird.example.invalid"
USER, OTHER = 7601, 7602
INSTALLATION = {USER: 8601, OTHER: 8602}


def _address(user: int) -> str:
    return f"user-{user}" + "@" + "example.invalid"


@pytest.fixture
def recipients(session: Session) -> Session:
    """Two users, each with a personal installation they can see, an address and warnings on."""
    for user in (USER, OTHER):
        session.add(GithubUser(id=user, login=f"example-{user}", last_sign_in_at=NOW))
        session.add(Account(id=user, login=f"example-{user}", type="User", github_plan="Free"))
        session.flush()
        session.add(Installation(id=INSTALLATION[user], account_id=user, created_at=NOW))
        session.flush()
        session.add_all(
            [
                UserInstallation(user_id=user, installation_id=INSTALLATION[user]),
                UserEmail(user_id=user, address=_address(user), read_at=NOW),
                EmailPreference(user_id=user, kind="usage_warnings", enabled=True, updated_at=NOW),
            ]
        )
    session.flush()
    return session


@pytest.fixture(autouse=True)
def fresh_cap_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts as a new worker process would: no cap ERROR logged yet."""
    monkeypatch.setattr(outbox._CAP_ERRORS, "_last", None)


@pytest.fixture
def sender() -> RecordingSender:
    return RecordingSender()


@pytest.fixture
def mail(sender: RecordingSender) -> Mail:
    return Mail(sender=sender, origin=ORIGIN, environment=example_environment())


def _queue(
    session: Session,
    key: str,
    *,
    user: int = USER,
    kind: str = "usage_warnings",
    account: int | None = USER,
    expires_at: datetime = NOW + timedelta(days=1),
) -> uuid.UUID:
    assert outbox.enqueue(
        session,
        user_id=user,
        account_id=account,
        kind=kind,
        template="run_out_warning",
        dedupe_key=key,
        params={"account": account},
        now=NOW,
        expires_at=expires_at,
    )
    return session.scalars(select(EmailOutbox.id).where(EmailOutbox.dedupe_key == key)).one()


def _row(session: Session, row_id: uuid.UUID) -> EmailOutbox:
    session.expire_all()
    return session.get_one(EmailOutbox, row_id)


def _sent_before(session: Session, user: int, n: int, at: datetime) -> None:
    """Rows already sent to the user at ``at``, as the caps count them."""
    for i in range(n):
        row_id = _queue(session, f"example:earlier:{user}:{i}:{at.isoformat()}", user=user)
        row = _row(session, row_id)
        row.state, row.finished_at = "sent", at
    session.flush()


# ---------- enqueue and the dedupe key ----------


def test_a_second_enqueue_of_the_same_email_is_a_no_op(recipients: Session) -> None:
    _queue(recipients, "example:once")
    assert not outbox.enqueue(
        recipients,
        user_id=USER,
        account_id=USER,
        kind="usage_warnings",
        template="run_out_warning",
        dedupe_key="example:once",
        params={},
        now=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    assert len(recipients.scalars(select(EmailOutbox)).all()) == 1


def test_the_run_out_warning_key_names_the_warning_allowance_cycle_and_recipient() -> None:
    key = outbox.run_out_warning_key("used_75", 123, "blacksmith", date(2026, 11, 1), 9)
    assert key == "run_out_warning:used_75:123:blacksmith:2026-11-01:9"
    others = {
        outbox.run_out_warning_key("used_90", 123, "blacksmith", date(2026, 11, 1), 9),
        outbox.run_out_warning_key("used_75", 124, "blacksmith", date(2026, 11, 1), 9),
        outbox.run_out_warning_key("used_75", 123, "github", date(2026, 11, 1), 9),
        outbox.run_out_warning_key("used_75", 123, "blacksmith", date(2026, 12, 1), 9),
        outbox.run_out_warning_key("used_75", 123, "blacksmith", date(2026, 11, 1), 10),
    }
    assert key not in others
    assert len(others) == 5


# ---------- the lease ----------


def test_a_claim_leases_the_row_and_a_lease_that_runs_out_is_taken_over(
    sessions: sessionmaker[Session], recipients: Session
) -> None:
    row_id = _queue(recipients, "example:lease")
    first = outbox.claim(sessions, NOW)
    assert first == outbox.Claimed(row_id, NOW + outbox.LEASE, 1)
    assert outbox.claim(sessions, NOW + outbox.LEASE - timedelta(seconds=1)) is None
    taken_over = outbox.claim(sessions, NOW + outbox.LEASE)
    assert taken_over == outbox.Claimed(row_id, NOW + 2 * outbox.LEASE, 2)


def test_a_send_whose_lease_was_taken_over_sends_nothing(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    _queue(recipients, "example:taken")
    stale = outbox.claim(sessions, NOW)
    assert stale is not None
    assert outbox.claim(sessions, NOW + outbox.LEASE) is not None
    outbox.send(sessions, mail, stale, NOW + outbox.LEASE)
    assert sender.sent == []


# ---------- one send, each outcome ----------


def test_a_usage_email_is_sent_once_with_its_one_click_link(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:sent")
    assert outbox.run(sessions, mail, NOW) == 1
    [message] = sender.sent
    assert message.reference == row_id
    assert message.to == _address(USER)
    assert message.subject == f"Example warning for {USER}"
    assert message.unsubscribe_url is not None
    token = message.unsubscribe_url.removeprefix(f"{ORIGIN}/email/unsubscribe/")
    row = _row(recipients, row_id)
    assert (row.state, row.outcome, row.provider_request_id) == ("sent", "http:200", "req-example")
    assert row.finished_at == NOW
    assert row.unsubscribe_sha256 == hashlib.sha256(token.encode()).digest()
    assert outbox.run(sessions, mail, NOW + timedelta(days=1)) == 0
    assert len(sender.sent) == 1


def test_a_service_email_has_no_unsubscribe_link(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:service", kind="service", account=None)
    outbox.run(sessions, mail, NOW)
    [message] = sender.sent
    assert message.unsubscribe_url is None
    assert _row(recipients, row_id).unsubscribe_sha256 is None


@pytest.mark.parametrize("code", ["timeout", "http:500", "http:502", "http:504"])
def test_an_unknown_outcome_is_final_and_never_sent_again(
    sessions: sessionmaker[Session],
    recipients: Session,
    mail: Mail,
    sender: RecordingSender,
    code: str,
) -> None:
    # The provider may have taken it: a missed warning is better than a double one.
    row_id = _queue(recipients, "example:unknown")
    sender.results.append(Result("unknown", code))
    outbox.run(sessions, mail, NOW)
    row = _row(recipients, row_id)
    assert (row.state, row.outcome, row.finished_at) == ("unknown", code, NOW)
    # Its link may have arrived, so it keeps working.
    assert row.unsubscribe_sha256 is not None
    assert outbox.run(sessions, mail, NOW + timedelta(days=1)) == 0
    assert len(sender.sent) == 1


def test_a_refused_email_is_failed_and_final(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:failed")
    sender.results.append(Result("failed", "http:422"))
    outbox.run(sessions, mail, NOW)
    row = _row(recipients, row_id)
    assert (row.state, row.outcome, row.finished_at) == ("failed", "http:422", NOW)


def test_a_request_that_never_arrived_is_retried_with_a_growing_wait_then_failed(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:retry")
    when = NOW
    for attempt in range(1, outbox.MAX_ATTEMPTS):
        sender.results.append(Result("retry", "connect"))
        assert outbox.run(sessions, mail, when) == 1
        row = _row(recipients, row_id)
        wait = min(outbox.BACKOFF_BASE * 2**attempt, outbox.BACKOFF_CAP)
        assert (row.state, row.attempts, row.retry_at) == ("queued", attempt, when + wait)
        when = when + wait
    sender.results.append(Result("retry", "connect"))
    outbox.run(sessions, mail, when)
    row = _row(recipients, row_id)
    assert (row.state, row.outcome, row.finished_at) == ("failed", "connect", when)


def test_the_daily_limit_puts_the_email_off_until_the_reset_without_counting(
    sessions: sessionmaker[Session],
    recipients: Session,
    mail: Mail,
    sender: RecordingSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    row_id = _queue(recipients, "example:reset")
    sender.results.append(Result("reset", "zoho:SM_151"))
    with caplog.at_level(logging.ERROR, logger=outbox.__name__):
        outbox.run(sessions, mail, NOW)
    row = _row(recipients, row_id)
    assert (row.state, row.attempts) == ("queued", 0)
    assert row.retry_at == datetime(2026, 11, 17, 18, 30, tzinfo=UTC)
    assert "zoho:SM_151" in caplog.text


def test_a_configuration_error_pauses_every_send_for_an_hour(
    sessions: sessionmaker[Session],
    recipients: Session,
    mail: Mail,
    sender: RecordingSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    row_id = _queue(recipients, "example:config")
    other_id = _queue(recipients, "example:config:other", user=OTHER, account=OTHER)
    sender.results.append(Result("config", "zoho:SM_111"))
    with caplog.at_level(logging.ERROR, logger=outbox.__name__):
        assert outbox.run(sessions, mail, NOW) == 1
    assert "zoho:SM_111" in caplog.text
    row = _row(recipients, row_id)
    assert (row.state, row.attempts) == ("queued", 0)
    assert recipients.get_one(WorkerTask, outbox.TASK).next_run_at == NOW + outbox.CONFIG_PAUSE
    assert outbox.run(sessions, mail, NOW + timedelta(minutes=59)) == 0
    assert len(sender.sent) == 1
    assert outbox.run(sessions, mail, NOW + outbox.CONFIG_PAUSE) == 2
    assert {_row(recipients, r).state for r in (row_id, other_id)} == {"sent"}


# ---------- what is checked before a send ----------


def test_an_email_past_its_expiry_is_skipped(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:expired", expires_at=NOW)
    outbox.run(sessions, mail, NOW)
    assert sender.sent == []
    assert (_row(recipients, row_id).state, _row(recipients, row_id).outcome) == (
        "skipped",
        "expired",
    )


def test_a_user_with_no_address_is_skipped(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:no-address")
    recipients.delete(recipients.get_one(UserEmail, USER))
    recipients.flush()
    outbox.run(sessions, mail, NOW)
    assert sender.sent == []
    assert _row(recipients, row_id).outcome == "no_address"


def test_a_suppressed_address_is_skipped(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:suppressed")
    address = recipients.get_one(UserEmail, USER)
    address.suppressed_at, address.suppressed_reason = NOW, "hard_bounce"
    recipients.flush()
    outbox.run(sessions, mail, NOW)
    assert sender.sent == []
    assert _row(recipients, row_id).outcome == "suppressed"


def test_warnings_turned_off_skip_a_usage_email_but_not_a_service_email(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    usage = _queue(recipients, "example:off")
    service = _queue(recipients, "example:service-off", kind="service", account=None)
    recipients.get_one(EmailPreference, (USER, "usage_warnings")).enabled = False
    recipients.flush()
    outbox.run(sessions, mail, NOW)
    assert _row(recipients, usage).outcome == "off"
    assert [message.reference for message in sender.sent] == [service]


def test_an_email_about_an_account_the_user_can_no_longer_see_is_skipped(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:no-access")
    recipients.delete(recipients.get_one(UserInstallation, (USER, INSTALLATION[USER])))
    recipients.flush()
    outbox.run(sessions, mail, NOW)
    assert sender.sent == []
    assert _row(recipients, row_id).outcome == "no_access"


# ---------- the caps ----------


def test_at_the_global_cap_an_email_waits_for_the_window_and_is_not_dropped(
    sessions: sessionmaker[Session],
    recipients: Session,
    mail: Mail,
    sender: RecordingSender,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(outbox, "GLOBAL_CAP", 2)
    _sent_before(recipients, OTHER, 1, NOW - timedelta(hours=20))
    _sent_before(recipients, OTHER, 1, NOW - timedelta(hours=10))
    # Outside the rolling window: not counted.
    _sent_before(recipients, OTHER, 3, NOW - timedelta(hours=25))
    first = _queue(recipients, "example:capped:1")
    second = _queue(recipients, "example:capped:2")
    with caplog.at_level(logging.ERROR, logger=outbox.__name__):
        outbox.run(sessions, mail, NOW)
        outbox.run(sessions, mail, NOW + timedelta(minutes=10))
    assert sender.sent == []
    for row_id in (first, second):
        row = _row(recipients, row_id)
        assert (row.state, row.attempts) == ("queued", 0)
    # The oldest send in the window leaves it 24 hours after it went.
    assert _row(recipients, first).retry_at == NOW + timedelta(hours=4)
    # One ERROR an hour, however many emails wait.
    assert len([r for r in caplog.records if r.name == outbox.__name__]) == 1
    outbox.run(sessions, mail, NOW + timedelta(hours=4))
    assert len(sender.sent) == 1


def test_at_the_recipient_cap_only_that_recipient_waits(
    sessions: sessionmaker[Session],
    recipients: Session,
    mail: Mail,
    sender: RecordingSender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(outbox, "RECIPIENT_CAP", 1)
    _sent_before(recipients, USER, 1, NOW - timedelta(hours=1))
    waiting = _queue(recipients, "example:recipient:1")
    other = _queue(recipients, "example:recipient:2", user=OTHER, account=OTHER)
    outbox.run(sessions, mail, NOW)
    assert [message.reference for message in sender.sent] == [other]
    row = _row(recipients, waiting)
    assert (row.state, row.retry_at) == ("queued", NOW + timedelta(hours=23))


# ---------- locks ----------


@pytest.fixture
def statements(connection: Connection) -> Iterator[list[str]]:
    sent: list[str] = []

    def record(*args: object) -> None:
        sent.append(str(args[2]))

    event.listen(connection, "before_cursor_execute", record)
    yield sent
    event.remove(connection, "before_cursor_execute", record)


def test_a_send_locks_the_task_row_then_the_outbox_row_and_never_the_address(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, statements: list[str]
) -> None:
    _queue(recipients, "example:locks")
    statements.clear()
    outbox.run(sessions, mail, NOW)
    locks = [
        found[1]
        for s in statements
        if s.startswith("SELECT")
        and " FOR UPDATE" in s
        and (found := re.search(r"\bFROM (\w+)", s))
    ]
    assert locks[-2:] == ["worker_task", "email_outbox"]
    assert not [s for s in statements if "FROM user_email" in s and " FOR " in s]


# ---------- what must never loop or send twice ----------


def test_the_daily_limit_pauses_every_send_until_the_reset(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    # The provider said stop: no further POST before its reset, whatever is queued.
    first = _queue(recipients, "example:limit:1")
    second = _queue(recipients, "example:limit:2", user=OTHER, account=OTHER)
    sender.results.append(Result("reset", "zoho:SMI_115"))
    assert outbox.run(sessions, mail, NOW) == 1
    reset = datetime(2026, 11, 17, 18, 30, tzinfo=UTC)
    assert recipients.get_one(WorkerTask, outbox.TASK).next_run_at == reset
    assert outbox.run(sessions, mail, reset - timedelta(minutes=1)) == 0
    assert len(sender.sent) == 1
    assert outbox.run(sessions, mail, reset) == 2
    assert {_row(recipients, r).state for r in (first, second)} == {"sent"}


def test_a_sender_that_raises_is_an_unknown_outcome_never_sent_twice(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:raises")

    def broken() -> None:
        raise RuntimeError("example")

    sender.on_send = broken
    outbox.run(sessions, mail, NOW)
    row = _row(recipients, row_id)
    assert (row.state, row.outcome) == ("unknown", "error")
    assert outbox.run(sessions, mail, NOW + outbox.LEASE) == 0
    assert len(sender.sent) == 1


def test_an_email_that_cannot_be_rendered_fails_once(
    sessions: sessionmaker[Session], recipients: Session, sender: RecordingSender
) -> None:
    # No run_out_warning template exists outside the tests yet.
    row_id = _queue(recipients, "example:render")
    outbox.run(sessions, Mail(sender=sender, origin=ORIGIN), NOW)
    row = _row(recipients, row_id)
    assert (row.state, row.outcome, row.finished_at) == ("failed", "render", NOW)
    assert sender.sent == []


def test_a_time_without_a_timezone_is_refused(sessions: sessionmaker[Session], mail: Mail) -> None:
    with pytest.raises(ValueError, match="timezone"):
        outbox.run(sessions, mail, NOW.replace(tzinfo=None))
