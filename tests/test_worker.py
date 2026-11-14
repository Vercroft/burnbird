import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx2
import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird import worker
from burnbird.github import events, reconcile
from burnbird.github.client import GithubApp
from burnbird.models import WebhookDelivery, WorkerTask
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

    monkeypatch.setattr(events, "process_pending", deliveries)
    monkeypatch.setattr(reconcile, "run", tasks)
    run_once(sessions, github, NOW)
    assert steps == ["deliveries", "reconcile"]


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
