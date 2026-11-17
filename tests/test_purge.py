"""The daily purge task: claimed once a day, even by two workers, and each purge runs in it."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from burnbird import purge, retention
from burnbird.models import WorkerTask
from burnbird.retention import Purged

NOW = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)


def test_the_purge_is_claimed_once_a_day(sessions: sessionmaker[Session]) -> None:
    assert purge.run(sessions, NOW)
    assert not purge.run(sessions, NOW + timedelta(hours=23))
    assert purge.run(sessions, NOW + timedelta(days=1))


def test_a_run_purges_at_its_own_time_and_records_its_success(
    sessions: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[datetime] = []

    def purges(_: Session, now: datetime) -> Purged:
        ran.append(now)
        return Purged(jobs=0, deliveries=0, sessions=0, emails=0, outbox_emails=0)

    monkeypatch.setattr(retention, "run_purges", purges)
    assert purge.run(sessions, NOW)
    assert ran == [NOW]
    assert session.get_one(WorkerTask, purge.TASK).last_success_at == NOW


def test_two_workers_purging_at_once_give_one_run(engine: Engine) -> None:
    # Two connections need committed rows; the task's row is deleted again at the end. The first
    # worker's claim stays uncommitted, so the second waits on its row and then finds it not due.
    try:
        with engine.connect() as first, engine.connect() as second:
            held = first.begin()
            first_sessions = sessionmaker(bind=first, join_transaction_mode="create_savepoint")
            assert purge.run(first_sessions, NOW)
            with ThreadPoolExecutor(max_workers=1) as pool:
                started = time.monotonic()
                racing = pool.submit(purge.run, sessionmaker(bind=second), NOW)
                time.sleep(0.3)
                assert not racing.done()
                held.commit()
                assert racing.result(timeout=10) is False
                assert time.monotonic() - started >= 0.3
    finally:
        with engine.begin() as connection:
            connection.execute(delete(WorkerTask).where(WorkerTask.name == purge.TASK))
        with Session(engine) as check:
            assert check.scalar(select(WorkerTask).where(WorkerTask.name == purge.TASK)) is None
