import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird import purge, worker
from burnbird.email import outbox
from burnbird.email.outbox import Mail
from burnbird.github import backfill, events, reconcile, redeliver
from burnbird.github.client import GithubApp
from burnbird.models import (
    Account,
    EmailPreference,
    GithubUser,
    Installation,
    Repository,
    UserEmail,
    UserInstallation,
    UserSession,
    WebhookDelivery,
    WorkerTask,
)
from burnbird.worker import run_once
from tests.email.examples import (
    RecordingSender,
    example_environment,
    example_params,
    example_usage,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def mail() -> Mail:
    """A sender that records instead of sending: no email leaves a test."""
    return Mail(
        sender=RecordingSender(),
        origin="https://burnbird.example.invalid",
        environment=example_environment(),
        run_out_warnings=True,
    )


@pytest.fixture
def answers() -> list[httpx2.Response]:
    """What the fake GitHub answers, in order; an empty installation list once they run out."""
    return []


@pytest.fixture
def github(rsa_key: RSAKey, answers: list[httpx2.Response]) -> Iterator[GithubApp]:
    def handle(request: httpx2.Request) -> httpx2.Response:
        return answers.pop(0) if answers else httpx2.Response(200, json=[])

    app = GithubApp(
        "Iv23.example",
        rsa_key,
        transport=httpx2.MockTransport(handle),
        clock=lambda: NOW,
        sleep=lambda _: None,
    )
    yield app
    app.close()


def test_one_pass_processes_the_due_deliveries_then_the_due_tasks(
    sessions: sessionmaker[Session],
    session: Session,
    github: GithubApp,
    mail: Mail,
) -> None:
    for n in (1, 2):
        session.execute(
            insert(WebhookDelivery).values(
                id=uuid.UUID(int=n),
                received_at=NOW,
                event="installation",
                action="unsuspend",
                payload={"installation": {"id": 9999}},
            )
        )
    session.flush()
    assert run_once(sessions, github, NOW, mail=mail) == 2
    assert session.get_one(WorkerTask, reconcile.TASK).next_run_at == NOW + reconcile.INTERVAL
    assert run_once(sessions, github, NOW, mail=mail) == 0


def test_deliveries_come_before_tasks(
    sessions: sessionmaker[Session],
    github: GithubApp,
    monkeypatch: pytest.MonkeyPatch,
    mail: Mail,
) -> None:
    steps: list[str] = []

    def deliveries(*_: object) -> int:
        steps.append("deliveries")
        return 0

    def tasks(*_: object) -> bool:
        steps.append("reconcile")
        return True

    def backfills(*_: object) -> bool:
        steps.append("backfill")
        return True

    def redeliveries(*_: object) -> bool:
        steps.append("redeliver")
        return True

    def emails(*_: object) -> int:
        steps.append("email-send")
        return 0

    def purges(*_: object) -> bool:
        steps.append("purge")
        return True

    monkeypatch.setattr(events, "process_pending", deliveries)
    monkeypatch.setattr(reconcile, "run", tasks)
    monkeypatch.setattr(redeliver, "run", redeliveries)
    monkeypatch.setattr(backfill, "run_next", backfills)
    monkeypatch.setattr(outbox, "run", emails)
    monkeypatch.setattr(purge, "run", purges)
    run_once(sessions, github, NOW, mail=mail)
    assert steps == ["deliveries", "reconcile", "redeliver", "backfill", "email-send", "purge"]


def test_a_failed_step_is_logged_and_the_next_still_runs(
    sessions: sessionmaker[Session],
    github: GithubApp,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mail: Mail,
) -> None:
    ran: list[str] = []

    def broken(*_: object) -> int:
        raise RuntimeError("the database went away")

    def tasks(*_: object) -> bool:
        ran.append("reconcile")
        return True

    monkeypatch.setattr(events, "process_pending", broken)
    monkeypatch.setattr(reconcile, "run", tasks)
    assert run_once(sessions, github, NOW, mail=mail) == 0
    assert ran == ["reconcile"]
    assert "worker: the deliveries step failed" in caplog.text


def test_a_failed_reconciliation_is_logged_and_the_pass_returns(
    sessions: sessionmaker[Session],
    github: GithubApp,
    answers: list[httpx2.Response],
    caplog: pytest.LogCaptureFixture,
    mail: Mail,
) -> None:
    answers.extend(httpx2.Response(500) for _ in range(3))
    with caplog.at_level(logging.ERROR):
        assert run_once(sessions, github, NOW, mail=mail) == 0
    [record] = [r for r in caplog.records if r.name == worker.__name__]
    assert record.getMessage() == "worker: the reconcile step failed"
    assert record.exc_info is not None


def test_a_pass_purges_what_is_past_its_limit_once_a_day(
    sessions: sessionmaker[Session],
    session: Session,
    github: GithubApp,
    mail: Mail,
) -> None:
    session.add(GithubUser(id=7301, login="example-purged", last_sign_in_at=NOW))
    session.flush()
    session.add(_expired_session(1, NOW))
    session.flush()
    run_once(sessions, github, NOW, mail=mail)
    session.expire_all()
    assert session.get(UserSession, bytes([1]) * 32) is None
    # The next one expires within the day: the purge is not due again until a day has passed.
    session.add(_expired_session(2, NOW + timedelta(hours=1)))
    session.flush()
    run_once(sessions, github, NOW + timedelta(hours=2), mail=mail)
    session.expire_all()
    assert session.get(UserSession, bytes([2]) * 32) is not None
    run_once(sessions, github, NOW + timedelta(days=1), mail=mail)
    session.expire_all()
    assert session.get(UserSession, bytes([2]) * 32) is None


def _expired_session(n: int, expires_at: datetime) -> UserSession:
    return UserSession(
        id_sha256=bytes([n]) * 32,
        user_id=7301,
        created_at=expires_at - timedelta(hours=8),
        expires_at=expires_at,
    )


def test_a_pass_sends_the_queued_emails(
    sessions: sessionmaker[Session], session: Session, github: GithubApp, mail: Mail
) -> None:
    session.add(GithubUser(id=7302, login="example-recipient", last_sign_in_at=NOW))
    session.add(
        Account(id=7302, login="example-recipient", type="User", github_plan="GitHub Free")
    )
    session.flush()
    session.add(Installation(id=8302, account_id=7302, created_at=NOW))
    session.flush()
    session.add(Repository(id=9302, installation_id=8302, name="app", private=True))
    session.flush()
    session.add_all(
        [
            UserInstallation(user_id=7302, installation_id=8302),
            UserEmail(user_id=7302, address="r" + "@" + "example.invalid", read_at=NOW),
            example_usage(9302, 9302, NOW - timedelta(hours=1)),
        ]
    )
    outbox.enqueue(
        session,
        user_id=7302,
        account_id=7302,
        kind="service",
        template="run_out_warning",
        dedupe_key="example:worker",
        params=example_params(NOW),
        now=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    session.flush()
    run_once(sessions, github, NOW, mail=mail)
    assert isinstance(mail.sender, RecordingSender)
    assert len(mail.sender.sent) == 1


def test_a_pass_gives_the_email_step_the_worker_s_github_client(
    sessions: sessionmaker[Session], session: Session, rsa_key: RSAKey
) -> None:
    # The mail it is given has no client; the warning's repository is named only after the
    # worker's own client asked GitHub.
    session.add(GithubUser(id=7303, login="example-holder", last_sign_in_at=NOW))
    session.add(Account(id=7303, login="example-holder", type="User", github_plan="GitHub Free"))
    session.flush()
    session.add(Installation(id=8303, account_id=7303, created_at=NOW))
    session.flush()
    session.add(Repository(id=9303, installation_id=8303, name="example-named", private=True))
    session.flush()
    session.add_all(
        [
            UserInstallation(user_id=7303, installation_id=8303),
            UserEmail(user_id=7303, address="o" + "@" + "example.invalid", read_at=NOW),
            EmailPreference(user_id=7303, kind="usage_warnings", enabled=True, updated_at=NOW),
            example_usage(9303, 9303, NOW - timedelta(hours=1)),
        ]
    )
    session.flush()
    params = example_params(NOW) | {"repository_id": 9303, "runner_class": "linux-x64-2vcpu"}
    outbox.enqueue(
        session,
        user_id=7303,
        account_id=7303,
        kind="usage_warnings",
        template="run_out_warning",
        dedupe_key="example:worker:named",
        params=params,
        now=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    session.flush()

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and request.url.path.endswith("/access_tokens"):
            expires = (NOW + timedelta(hours=1)).isoformat()
            return httpx2.Response(
                201, json={"token": "t" + uuid.uuid4().hex, "expires_at": expires}
            )
        if request.url.path.endswith("/permission"):
            return httpx2.Response(200, json={"permission": "admin"})
        return httpx2.Response(200, json=[])

    app = GithubApp(
        "Iv23.example",
        rsa_key,
        transport=httpx2.MockTransport(handle),
        clock=lambda: NOW,
        sleep=lambda _: None,
    )
    sender = RecordingSender()
    mail = Mail(
        sender=sender,
        origin="https://burnbird.example.invalid",
        environment=example_environment(),
        run_out_warnings=True,
    )
    run_once(sessions, app, NOW, mail=mail)
    app.close()
    [message] = sender.sent
    assert "example-named" in message.text
