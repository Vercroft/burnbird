"""The outbox: an email is queued once, leased, checked, sent at most once, and capped.

Every test enqueues its own rows; the provider is a recording fake, so no email leaves.
"""

import hashlib
import itertools
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from jinja2 import ChoiceLoader, DictLoader
from joserfc.jwk import RSAKey
from sqlalchemy import Connection, delete, event, select, update
from sqlalchemy.orm import Session, sessionmaker

from burnbird import logs, retention
from burnbird.email import outbox
from burnbird.email.outbox import Mail
from burnbird.email.sender import Result
from burnbird.github.client import GithubApp
from burnbird.models import (
    Account,
    EmailOutbox,
    EmailPreference,
    GithubUser,
    Installation,
    Job,
    Repository,
    UserEmail,
    UserInstallation,
    WorkerTask,
)
from burnbird.templates import ENVIRONMENT
from tests.email.examples import RecordingSender, example_environment

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
ORIGIN = "https://burnbird.example.invalid"
USER, OTHER = 7601, 7602
INSTALLATION = {USER: 8601, OTHER: 8602}
# Installed long before NOW's cycle (1 to 30 November 2026), so every day of it is complete.
INSTALLED = datetime(2026, 9, 1, tzinfo=UTC)
CYCLE_START = date(2026, 11, 1)
_JOB_IDS = itertools.count(1)


def _address(user: int) -> str:
    return f"user-{user}" + "@" + "example.invalid"


def _use(session: Session, user: int, minutes: dict[date, int]) -> int:
    """Store Blacksmith jobs of the given minutes per day in a repository of the user's account."""
    repository_id = 9000 + user
    if session.get(Repository, repository_id) is None:
        session.add(
            Repository(
                id=repository_id,
                installation_id=INSTALLATION[user],
                name="app",
                private=True,
                backfilled_at=INSTALLED,
            )
        )
        session.flush()
    for day, amount in minutes.items():
        completed = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=12)
        job_id = next(_JOB_IDS)
        session.add(
            Job(
                id=job_id,
                run_attempt=1,
                run_id=job_id,
                repository_id=repository_id,
                labels=["blacksmith-2vcpu-ubuntu-2404"],
                created_at=completed,
                started_at=completed - timedelta(minutes=amount),
                completed_at=completed,
                provider="blacksmith",
                runner_class="linux-x64-2vcpu",
                billed_minutes=Decimal(amount),
                amount=Decimal(amount),
            )
        )
    session.flush()
    return repository_id


@pytest.fixture
def recipients(session: Session) -> Session:
    """Two users, each with a personal installation they can see, an address and warnings on.

    Each account has used 2,400 of its 3,000 free Blacksmith minutes (80%) on 1 November, so a
    used-75 warning, the rows' default, is still true at send.
    """
    for user in (USER, OTHER):
        session.add(GithubUser(id=user, login=f"example-{user}", last_sign_in_at=NOW))
        session.add(Account(id=user, login=f"example-{user}", type="User", github_plan="Free"))
        session.flush()
        session.add(Installation(id=INSTALLATION[user], account_id=user, created_at=INSTALLED))
        session.flush()
        session.add_all(
            [
                UserInstallation(user_id=user, installation_id=INSTALLATION[user]),
                UserEmail(user_id=user, address=_address(user), read_at=NOW),
                EmailPreference(user_id=user, kind="usage_warnings", enabled=True, updated_at=NOW),
            ]
        )
        session.flush()
        _use(session, user, {CYCLE_START: 2400})
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
    return Mail(
        sender=sender, origin=ORIGIN, environment=example_environment(), run_out_warnings=True
    )


def _params(
    kind: str = "used-75",
    *,
    provider: str = "blacksmith",
    cycle_start: date = CYCLE_START,
    repository_id: int | None = None,
    runner_class: str | None = None,
) -> dict[str, Any]:
    """A run-out warning's params: only the provider, kind, cycle start and two IDs."""
    return {
        "provider": provider,
        "kind": kind,
        "cycle_start": cycle_start.isoformat(),
        "repository_id": repository_id,
        "runner_class": runner_class,
    }


def _queue(
    session: Session,
    key: str,
    *,
    user: int = USER,
    kind: str = "usage_warnings",
    account: int | None = USER,
    expires_at: datetime = NOW + timedelta(days=1),
    params: dict[str, Any] | None = None,
) -> uuid.UUID:
    assert outbox.enqueue(
        session,
        user_id=user,
        account_id=account,
        kind=kind,
        template="run_out_warning",
        dedupe_key=key,
        params=_params() if params is None else params,
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
    assert message.subject == "Example used-75 warning"
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
    row_id = _queue(recipients, "example:service", kind="service")
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
    service = _queue(recipients, "example:service-off", kind="service")
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


# ---------- freshness and the run-out forecast, again at send ----------


@pytest.mark.parametrize("kind", ["usage_warnings", "service"])
def test_an_address_past_its_freshness_is_not_mailed(
    sessions: sessionmaker[Session],
    recipients: Session,
    mail: Mail,
    sender: RecordingSender,
    kind: str,
) -> None:
    # The daily purge deletes an address not read for EMAIL_FRESHNESS; until it runs, the send
    # checks the same rule itself. One just inside it is still mailed.
    stale = _queue(recipients, "example:stale", kind=kind)
    fresh = _queue(recipients, "example:fresh", user=OTHER, account=OTHER, kind=kind)
    edge = NOW - retention.EMAIL_FRESHNESS
    recipients.get_one(UserEmail, USER).read_at = edge - timedelta(seconds=1)
    recipients.get_one(UserEmail, OTHER).read_at = edge
    recipients.flush()
    outbox.run(sessions, mail, NOW)
    assert (_row(recipients, stale).state, _row(recipients, stale).outcome) == (
        "skipped",
        "stale_address",
    )
    assert [message.reference for message in sender.sent] == [fresh]


def test_a_run_out_forecast_no_longer_true_is_skipped(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    # 2,400 used on 1 November and 60 a day from 10 to 16 November: 2,820 used, 180 left at a
    # pace of 60 is reached on 19 November. The other account used nothing in its last 7 days:
    # no pace, so no run-out (its warning was queued before its jobs were deleted, say).
    _use(recipients, USER, {date(2026, 11, d): 60 for d in range(10, 17)})
    still = _queue(recipients, "example:still", params=_params("runs-out-before-reset"))
    gone = _queue(
        recipients,
        "example:gone",
        user=OTHER,
        account=OTHER,
        params=_params("runs-out-before-reset"),
    )
    outbox.run(sessions, mail, NOW)
    assert [message.reference for message in sender.sent] == [still]
    assert (_row(recipients, gone).state, _row(recipients, gone).outcome) == (
        "skipped",
        "no_longer_true",
    )


def test_a_ran_out_warning_the_state_no_longer_shows_is_skipped(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    # 600 more on 3 November uses up the 3,000; the other account is still at 2,400.
    _use(recipients, USER, {date(2026, 11, 3): 600})
    shown = _queue(recipients, "example:ran-out", params=_params("ran-out"))
    deleted = _queue(
        recipients, "example:ran-out:gone", user=OTHER, account=OTHER, params=_params("ran-out")
    )
    outbox.run(sessions, mail, NOW)
    assert [message.reference for message in sender.sent] == [shown]
    assert _row(recipients, deleted).outcome == "no_longer_true"


def test_a_percentage_warning_below_its_threshold_now_is_skipped(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    # Both accounts are at 2,400 of 3,000, 80%. The first then reaches 2,700, 90%; the other's
    # used-90 warning was queued before jobs were deleted, say.
    _use(recipients, USER, {date(2026, 11, 2): 300})
    reached = _queue(recipients, "example:90", params=_params("used-90"))
    below = _queue(
        recipients, "example:90:other", user=OTHER, account=OTHER, params=_params("used-90")
    )
    still_75 = _queue(recipients, "example:75:other", user=OTHER, account=OTHER)
    outbox.run(sessions, mail, NOW)
    assert {message.reference for message in sender.sent} == {reached, still_75}
    assert (_row(recipients, below).state, _row(recipients, below).outcome) == (
        "skipped",
        "no_longer_true",
    )
    # Below 75% too, after its jobs are deleted.
    recipients.execute(delete(Job).where(Job.repository_id == 9000 + OTHER))
    again = _queue(recipients, "example:75:again", user=OTHER, account=OTHER)
    outbox.run(sessions, mail, NOW)
    assert _row(recipients, again).outcome == "no_longer_true"


def test_a_changed_cycle_is_skipped_as_expired(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    # Queued for the October cycle; NOW is in November's (the cycle day can change too).
    row_id = _queue(recipients, "example:october", params=_params(cycle_start=date(2026, 10, 1)))
    outbox.run(sessions, mail, NOW)
    assert (_row(recipients, row_id).state, _row(recipients, row_id).outcome) == (
        "skipped",
        "expired",
    )
    assert sender.sent == []


@pytest.mark.parametrize(
    "params",
    [
        {"account": USER},
        _params() | {"login": "example-login"},
        _params(kind="used-50"),
        _params(kind="used-75", provider="github"),
        _params(provider="self-hosted"),
        _params() | {"cycle_start": "1 November"},
        _params() | {"repository_id": "5011"},
        _params() | {"repository_id": True},
        _params() | {"runner_class": 7},
        list(_params()),
        _params() | {"kind": ["used-75"]},
    ],
    ids=[
        "old-shape",
        "extra-key",
        "unknown-kind",
        "kind-github-does-not-get",
        "no-allowance",
        "not-a-date",
        "id-as-text",
        "id-as-bool",
        "runner-not-text",
        "a-list",
        "kind-as-list",
    ],
)
def test_a_row_whose_params_are_not_a_run_out_warning_s_fails_once(
    sessions: sessionmaker[Session],
    recipients: Session,
    mail: Mail,
    sender: RecordingSender,
    params: dict[str, Any] | list[str],
) -> None:
    row_id = _queue(recipients, "example:params")
    # Planted in the row as stored: the JSONB column takes any JSON, a list included.
    recipients.execute(update(EmailOutbox).where(EmailOutbox.id == row_id).values(params=params))
    outbox.run(sessions, mail, NOW)
    row = _row(recipients, row_id)
    assert (row.state, row.outcome, row.finished_at) == ("failed", "params", NOW)
    assert sender.sent == []


# ---------- the top repository, named only after a check outside the locks ----------

REPOSITORY_NAME = "example-repo-name"
LOGIN = f"example-{USER}"
RUNNER = "Ubuntu x64 2 vCPU"
type Answer = httpx2.Response | Exception


@pytest.fixture
def permission() -> list[Answer]:
    """GitHub's answers to the permission check; the last one answers every retry."""
    return []


@pytest.fixture
def github_requests() -> list[httpx2.Request]:
    return []


@pytest.fixture
def checking_mail(
    rsa_key: RSAKey,
    sender: RecordingSender,
    permission: list[Answer],
    github_requests: list[httpx2.Request],
) -> Iterator[Mail]:
    def handle(request: httpx2.Request) -> httpx2.Response:
        github_requests.append(request)
        first = permission[0] if permission else None
        if request.method == "POST":
            # 418 in the answers is the marker for a refused installation: its token is a 403.
            if isinstance(first, httpx2.Response) and first.status_code == 418:
                return httpx2.Response(403, json={"message": "suspended"})
            expires = (NOW + timedelta(hours=1)).isoformat()
            return httpx2.Response(
                201, json={"token": "t" + uuid.uuid4().hex, "expires_at": expires}
            )
        answer = permission.pop(0) if len(permission) > 1 else first
        if isinstance(answer, Exception):
            raise answer
        assert answer is not None
        return answer

    app = GithubApp(
        "Iv23.example",
        rsa_key,
        transport=httpx2.MockTransport(handle),
        clock=lambda: NOW,
        sleep=lambda _: None,
    )
    yield Mail(
        sender=sender,
        origin=ORIGIN,
        environment=example_environment(),
        github=app,
        run_out_warnings=True,
    )
    app.close()


def _top_queued(session: Session, user: int = USER) -> uuid.UUID:
    repository_id = 9000 + user
    session.execute(
        update(Repository).where(Repository.id == repository_id).values(name=REPOSITORY_NAME)
    )
    return _queue(
        session,
        f"example:top:{user}",
        user=user,
        account=user,
        params=_params(repository_id=repository_id, runner_class="linux-x64-2vcpu"),
    )


@pytest.mark.parametrize("answer", ["admin", "write", "read"])
def test_read_write_and_admin_name_the_repository(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    sender: RecordingSender,
    permission: list[Answer],
    github_requests: list[httpx2.Request],
    answer: str,
) -> None:
    permission.append(httpx2.Response(200, json={"permission": answer}))
    _top_queued(recipients)
    outbox.run(sessions, checking_mail, NOW)
    [message] = sender.sent
    assert REPOSITORY_NAME in message.text
    assert REPOSITORY_NAME in message.html
    [check] = [r for r in github_requests if r.method == "GET"]
    assert check.url.path == f"/repos/{LOGIN}/{REPOSITORY_NAME}/collaborators/{LOGIN}/permission"


FAILED_CHECKS = [
    pytest.param(httpx2.Response(200, json={"permission": "none"}), "permission:none", id="none"),
    pytest.param(httpx2.Response(404), "http:404", id="404"),
    pytest.param(httpx2.Response(403, json={"message": "Forbidden"}), "http:403", id="403"),
    pytest.param(httpx2.Response(500), "http:500", id="500"),
    pytest.param(httpx2.ReadTimeout("no answer"), "network", id="timeout"),
    pytest.param(httpx2.Response(429, headers={"retry-after": "60"}), "rate_limited", id="rate"),
    pytest.param(httpx2.Response(301), "http:301", id="moved"),
    pytest.param(httpx2.Response(418), "refused", id="refused-installation"),
]


@pytest.mark.parametrize(("answer", "code"), FAILED_CHECKS)
def test_none_404_403_500_a_timeout_and_a_rate_limit_leave_it_out(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    sender: RecordingSender,
    permission: list[Answer],
    answer: Answer,
    code: str,
) -> None:
    permission.append(answer)
    _top_queued(recipients)
    outbox.run(sessions, checking_mail, NOW)
    [message] = sender.sent
    assert REPOSITORY_NAME not in message.text
    assert REPOSITORY_NAME not in message.html


@pytest.mark.parametrize(("answer", "code"), FAILED_CHECKS)
def test_the_email_is_sent_when_the_check_fails(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    permission: list[Answer],
    answer: Answer,
    code: str,
) -> None:
    permission.append(answer)
    row_id = _top_queued(recipients)
    outbox.run(sessions, checking_mail, NOW)
    assert _row(recipients, row_id).state == "sent"


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(("answer", "code"), FAILED_CHECKS)
def test_a_failed_check_logs_one_warning_without_login_or_name(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    permission: list[Answer],
    caplog: pytest.LogCaptureFixture,
    answer: Answer,
    code: str,
) -> None:
    permission.append(answer)
    row_id = _top_queued(recipients)
    # The worker command's own logging set-up, at its most verbose, then every line captured.
    logs.configure(logging.DEBUG)
    logging.getLogger().addHandler(caplog.handler)
    caplog.handler.setLevel(logging.DEBUG)
    outbox.run(sessions, checking_mail, NOW)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        f"email: {row_id}: the repository is not named ({code})"
    ]
    for record in caplog.records:
        assert LOGIN not in record.getMessage()
        assert REPOSITORY_NAME not in record.getMessage()


@pytest.mark.usefixtures("restore_logging")
def test_any_other_failure_of_the_check_leaves_the_name_out_and_sends(
    sessions: sessionmaker[Session],
    recipients: Session,
    rsa_key: RSAKey,
    sender: RecordingSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A client already closed raises an error of its own, not GitHub's.
    app = GithubApp("Iv23.example", rsa_key, clock=lambda: NOW, sleep=lambda _: None)
    app.close()
    row_id = _top_queued(recipients)
    logs.configure(logging.DEBUG)
    logging.getLogger().addHandler(caplog.handler)
    caplog.handler.setLevel(logging.DEBUG)
    outbox.run(
        sessions, Mail(sender, ORIGIN, example_environment(), app, run_out_warnings=True), NOW
    )
    assert _row(recipients, row_id).state == "sent"
    assert REPOSITORY_NAME not in sender.sent[0].text
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [f"email: {row_id}: the repository is not named (error)"]


@pytest.mark.usefixtures("restore_logging")
def test_an_error_of_any_kind_leaves_the_name_out_and_sends(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    sender: RecordingSender,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An error no list of types foresees, such as one from signing the App's JWT.
    class UnforeseenError(Exception):
        pass

    def unforeseen(*args: object) -> str:
        raise UnforeseenError(REPOSITORY_NAME)

    assert checking_mail.github is not None
    monkeypatch.setattr(checking_mail.github, "collaborator_permission", unforeseen)
    row_id = _top_queued(recipients)
    logs.configure(logging.DEBUG)
    logging.getLogger().addHandler(caplog.handler)
    caplog.handler.setLevel(logging.DEBUG)
    outbox.run(sessions, checking_mail, NOW)
    assert _row(recipients, row_id).state == "sent"
    assert REPOSITORY_NAME not in sender.sent[0].text
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        f"email: {row_id}: the repository is not named (error)"
    ]
    assert REPOSITORY_NAME not in caplog.text


def test_an_answer_names_only_the_repository_it_was_asked_about(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    sender: RecordingSender,
    permission: list[Answer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The row's params change while GitHub answers: the answer is about the old repository.
    from burnbird.email.run_out import ask

    permission.append(httpx2.Response(200, json={"permission": "admin"}))
    row_id = _top_queued(recipients)

    def then_change(github: GithubApp | None, row: uuid.UUID, question: Any) -> object:
        answer = ask(github, row, question)
        changed = _params(repository_id=9000 + OTHER, runner_class="linux-x64-2vcpu")
        recipients.execute(
            update(EmailOutbox).where(EmailOutbox.id == row_id).values(params=changed)
        )
        return answer

    monkeypatch.setattr("burnbird.email.run_out.ask", then_change)
    outbox.run(sessions, checking_mail, NOW)
    [message] = sender.sent
    assert REPOSITORY_NAME not in message.text


def test_github_is_asked_only_about_a_row_that_may_go(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    github_requests: list[httpx2.Request],
) -> None:
    # One waits at the recipient cap; the other's user turned warnings off.
    _sent_before(recipients, USER, outbox.RECIPIENT_CAP, NOW - timedelta(hours=1))
    waiting = _top_queued(recipients)
    off = _top_queued(recipients, OTHER)
    recipients.get_one(EmailPreference, (OTHER, "usage_warnings")).enabled = False
    recipients.flush()
    outbox.run(sessions, checking_mail, NOW)
    assert github_requests == []
    assert (_row(recipients, waiting).state, _row(recipients, off).outcome) == ("queued", "off")


def test_the_runner_type_is_always_named(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    sender: RecordingSender,
    permission: list[Answer],
) -> None:
    permission.extend([httpx2.Response(404), httpx2.Response(200, json={"permission": "admin"})])
    _top_queued(recipients)
    _top_queued(recipients, OTHER)
    outbox.run(sessions, checking_mail, NOW)
    assert len(sender.sent) == 2
    assert all(RUNNER in message.text for message in sender.sent)
    assert sum(REPOSITORY_NAME in message.text for message in sender.sent) == 1


def test_no_repository_no_check_and_no_client_no_name(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    mail: Mail,
    sender: RecordingSender,
    github_requests: list[httpx2.Request],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _queue(recipients, "example:no-top")
    outbox.run(sessions, checking_mail, NOW)
    assert github_requests == []
    # A mail without a GitHub client never names the repository.
    row_id = _top_queued(recipients)
    with caplog.at_level(logging.WARNING):
        outbox.run(sessions, mail, NOW)
    assert REPOSITORY_NAME not in sender.sent[-1].text
    assert [r.getMessage() for r in caplog.records] == [
        f"email: {row_id}: the repository is not named (no_client)"
    ]


def test_a_row_that_will_not_go_is_not_checked(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    github_requests: list[httpx2.Request],
) -> None:
    # A row whose params are not a warning's, or that names no runner size, asks GitHub nothing.
    row_id = _top_queued(recipients)
    recipients.execute(
        update(EmailOutbox).where(EmailOutbox.id == row_id).values(params={"account": USER})
    )
    _queue(
        recipients,
        "example:no-runner",
        user=OTHER,
        account=OTHER,
        params=_params(repository_id=9000 + OTHER),
    )
    outbox.run(sessions, checking_mail, NOW)
    assert github_requests == []


# ---------- the run-out warnings switch ----------


def test_warnings_off_skips_a_queued_warning_as_disabled(
    sessions: sessionmaker[Session],
    recipients: Session,
    checking_mail: Mail,
    sender: RecordingSender,
    github_requests: list[httpx2.Request],
) -> None:
    # Turned off, a warning already queued is not sent either, and GitHub is not asked about it.
    row_id = _top_queued(recipients)
    outbox.run(sessions, replace(checking_mail, run_out_warnings=False), NOW)
    row = _row(recipients, row_id)
    assert (row.state, row.outcome, row.finished_at) == ("skipped", "disabled", NOW)
    assert sender.sent == []
    assert github_requests == []


def test_warnings_on_sends_a_queued_warning(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    row_id = _queue(recipients, "example:switch:on")
    outbox.run(sessions, replace(mail, run_out_warnings=True), NOW)
    assert [message.reference for message in sender.sent] == [row_id]


def test_the_switch_works_both_ways(
    sessions: sessionmaker[Session], recipients: Session, mail: Mail, sender: RecordingSender
) -> None:
    on, off = replace(mail, run_out_warnings=True), replace(mail, run_out_warnings=False)
    first = _queue(recipients, "example:switch:1")
    outbox.run(sessions, on, NOW)
    second = _queue(recipients, "example:switch:2", user=OTHER, account=OTHER)
    outbox.run(sessions, off, NOW)
    third = _queue(recipients, "example:switch:3")
    outbox.run(sessions, on, NOW)
    assert [message.reference for message in sender.sent] == [first, third]
    assert _row(recipients, second).outcome == "disabled"


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


def test_a_row_waiting_at_a_cap_does_not_read_the_state(
    sessions: sessionmaker[Session],
    recipients: Session,
    mail: Mail,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The state is a scan of the account's jobs under the send's locks: only a row that may go
    # now reads it.
    from burnbird.email.run_out import checked

    read: list[uuid.UUID] = []

    def counting(session: Session, row: EmailOutbox, now: datetime) -> object:
        read.append(row.id)
        return checked(session, row, now)

    monkeypatch.setattr("burnbird.email.run_out.checked", counting)
    _sent_before(recipients, USER, outbox.RECIPIENT_CAP, NOW - timedelta(hours=1))
    waiting = _queue(recipients, "example:capped:state")
    going = _queue(recipients, "example:going:state", user=OTHER, account=OTHER)
    outbox.run(sessions, mail, NOW)
    assert read == [going]
    assert _row(recipients, waiting).state == "queued"


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
    # A template that names a value the email does not have.
    assert ENVIRONMENT.loader is not None
    broken = ENVIRONMENT.overlay(
        loader=ChoiceLoader(
            [DictLoader({"email/run_out_warning.txt": "{{ missing }}"}), ENVIRONMENT.loader]
        )
    )
    row_id = _queue(recipients, "example:render")
    outbox.run(
        sessions,
        Mail(sender=sender, origin=ORIGIN, environment=broken, run_out_warnings=True),
        NOW,
    )
    row = _row(recipients, row_id)
    assert (row.state, row.outcome, row.finished_at) == ("failed", "render", NOW)
    assert sender.sent == []


def test_a_value_the_email_cannot_be_given_fails_it_once(
    sessions: sessionmaker[Session],
    recipients: Session,
    sender: RecordingSender,
    mail: Mail,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Building the email's values fails as a render does: once, never every lease.
    def broken(*args: object, **kwargs: object) -> dict[str, object]:
        raise ValueError("no value")

    monkeypatch.setattr(outbox, "_context", broken)
    row_id = _queue(recipients, "example:context")
    outbox.run(sessions, mail, NOW)
    row = _row(recipients, row_id)
    assert (row.state, row.outcome, row.finished_at) == ("failed", "render", NOW)
    assert sender.sent == []


def test_a_time_without_a_timezone_is_refused(sessions: sessionmaker[Session], mail: Mail) -> None:
    with pytest.raises(ValueError, match="timezone"):
        outbox.run(sessions, mail, NOW.replace(tzinfo=None))
