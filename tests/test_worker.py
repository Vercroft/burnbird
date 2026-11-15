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
from burnbird.github import backfill, events, reconcile, redeliver
from burnbird.github.client import GithubApp
from burnbird.models import GithubUser, UserSession, WebhookDelivery, WorkerTask
from burnbird.worker import run_once

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


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
    sessions: sessionmaker[Session], session: Session, github: GithubApp
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
    assert run_once(sessions, github, NOW) == 2
    assert session.get_one(WorkerTask, reconcile.TASK).next_run_at == NOW + reconcile.INTERVAL
    assert run_once(sessions, github, NOW) == 0


def test_deliveries_come_before_tasks(
    sessions: sessionmaker[Session], github: GithubApp, monkeypatch: pytest.MonkeyPatch
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

    def purges(*_: object) -> bool:
        steps.append("purge")
        return True

    monkeypatch.setattr(events, "process_pending", deliveries)
    monkeypatch.setattr(reconcile, "run", tasks)
    monkeypatch.setattr(redeliver, "run", redeliveries)
    monkeypatch.setattr(backfill, "run_next", backfills)
    monkeypatch.setattr(purge, "run", purges)
    run_once(sessions, github, NOW)
    assert steps == ["deliveries", "reconcile", "redeliver", "backfill", "purge"]


def test_a_failed_step_is_logged_and_the_next_still_runs(
    sessions: sessionmaker[Session],
    github: GithubApp,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ran: list[str] = []

    def broken(*_: object) -> int:
        raise RuntimeError("the database went away")

    def tasks(*_: object) -> bool:
        ran.append("reconcile")
        return True

    monkeypatch.setattr(events, "process_pending", broken)
    monkeypatch.setattr(reconcile, "run", tasks)
    assert run_once(sessions, github, NOW) == 0
    assert ran == ["reconcile"]
    assert "worker: the deliveries step failed" in caplog.text


def test_a_failed_reconciliation_is_logged_and_the_pass_returns(
    sessions: sessionmaker[Session],
    github: GithubApp,
    answers: list[httpx2.Response],
    caplog: pytest.LogCaptureFixture,
) -> None:
    answers.extend(httpx2.Response(500) for _ in range(3))
    with caplog.at_level(logging.ERROR):
        assert run_once(sessions, github, NOW) == 0
    [record] = [r for r in caplog.records if r.name == worker.__name__]
    assert record.getMessage() == "worker: the reconcile step failed"
    assert record.exc_info is not None


def test_a_pass_purges_what_is_past_its_limit_once_a_day(
    sessions: sessionmaker[Session], session: Session, github: GithubApp
) -> None:
    session.add(GithubUser(id=7301, login="example-purged", last_sign_in_at=NOW))
    session.flush()
    session.add(_expired_session(1, NOW))
    session.flush()
    run_once(sessions, github, NOW)
    session.expire_all()
    assert session.get(UserSession, bytes([1]) * 32) is None
    # The next one expires within the day: the purge is not due again until a day has passed.
    session.add(_expired_session(2, NOW + timedelta(hours=1)))
    session.flush()
    run_once(sessions, github, NOW + timedelta(hours=2))
    session.expire_all()
    assert session.get(UserSession, bytes([2]) * 32) is not None
    run_once(sessions, github, NOW + timedelta(days=1))
    session.expire_all()
    assert session.get(UserSession, bytes([2]) * 32) is None


def _expired_session(n: int, expires_at: datetime) -> UserSession:
    return UserSession(
        id_sha256=bytes([n]) * 32,
        user_id=7301,
        created_at=expires_at - timedelta(hours=8),
        expires_at=expires_at,
    )
