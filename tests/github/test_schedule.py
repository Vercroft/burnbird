import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, insert, select
from sqlalchemy.orm import Session, sessionmaker

from burnbird.github.schedule import Claim, claim, finish
from burnbird.models import WorkerTask

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _task(session: Session, name: str) -> WorkerTask:
    task = session.get(WorkerTask, name, populate_existing=True)
    assert task is not None
    return task


def test_a_task_never_run_is_due_at_once(
    sessions: sessionmaker[Session], session: Session
) -> None:
    assert claim(sessions, "a", NOW, HOUR) == Claim("a", NOW, None)
    assert _task(session, "a").next_run_at == NOW + HOUR


def test_a_claimed_run_is_not_due_again_before_its_interval(
    sessions: sessionmaker[Session],
) -> None:
    assert claim(sessions, "a", NOW, HOUR) is not None
    assert claim(sessions, "a", NOW, HOUR) is None
    assert claim(sessions, "a", NOW + HOUR - timedelta(microseconds=1), HOUR) is None
    assert claim(sessions, "a", NOW + HOUR, HOUR) is not None


def test_each_task_has_its_own_schedule(sessions: sessionmaker[Session]) -> None:
    assert claim(sessions, "a", NOW, HOUR) is not None
    assert claim(sessions, "b", NOW, HOUR) is not None


def test_a_complete_run_moves_last_success_to_its_start(
    sessions: sessionmaker[Session], session: Session
) -> None:
    run = claim(sessions, "a", NOW, HOUR)
    assert run is not None
    finish(sessions, run, complete=True)
    assert _task(session, "a").last_success_at == NOW
    later = claim(sessions, "a", NOW + HOUR, HOUR)
    assert later == Claim("a", NOW + HOUR, NOW)


def test_an_incomplete_run_leaves_last_success(
    sessions: sessionmaker[Session], session: Session
) -> None:
    first = claim(sessions, "a", NOW, HOUR)
    assert first is not None
    finish(sessions, first, complete=True)
    second = claim(sessions, "a", NOW + HOUR, HOUR)
    assert second is not None
    finish(sessions, second, complete=False)
    assert _task(session, "a").last_success_at == NOW


def test_a_late_finish_of_an_older_run_never_moves_last_success_back(
    sessions: sessionmaker[Session], session: Session
) -> None:
    old = claim(sessions, "a", NOW, HOUR)
    new = claim(sessions, "a", NOW + HOUR, HOUR)
    assert old is not None
    assert new is not None
    finish(sessions, new, complete=True)
    finish(sessions, old, complete=True)
    assert _task(session, "a").last_success_at == NOW + HOUR


def test_a_rate_limited_run_is_put_off_until_it_may_resume(
    sessions: sessionmaker[Session], session: Session
) -> None:
    run = claim(sessions, "a", NOW, HOUR)
    assert run is not None
    finish(sessions, run, complete=False, resume_at=NOW + 2 * HOUR)
    assert _task(session, "a").next_run_at == NOW + 2 * HOUR
    assert claim(sessions, "a", NOW + HOUR, HOUR) is None


def test_a_resume_time_before_the_next_run_keeps_the_interval(
    sessions: sessionmaker[Session], session: Session
) -> None:
    run = claim(sessions, "a", NOW, HOUR)
    assert run is not None
    finish(sessions, run, complete=False, resume_at=NOW + timedelta(minutes=5))
    assert _task(session, "a").next_run_at == NOW + HOUR


@pytest.mark.parametrize("existing", [True, False], ids=["due row", "first run"])
def test_two_workers_claiming_at_once_give_one_run(engine: Engine, existing: bool) -> None:
    # Two connections need committed rows; the task's row is deleted again at the end. With a due
    # row the two UPDATEs meet on it: the second waits for the first, then its WHERE is evaluated
    # again and no longer matches. On a first run the two INSERTs meet on the key instead.
    name = f"race-{uuid.uuid4()}"
    if existing:
        with engine.begin() as connection:
            connection.execute(insert(WorkerTask).values(name=name, next_run_at=NOW))
    try:
        with engine.connect() as first, engine.connect() as second:
            held = first.begin()
            # The first worker's claim stays uncommitted, so its row lock is held.
            first_sessions = sessionmaker(bind=first, join_transaction_mode="create_savepoint")
            assert claim(first_sessions, name, NOW, HOUR) is not None
            second_sessions = sessionmaker(bind=second)
            with ThreadPoolExecutor(max_workers=1) as pool:
                started = time.monotonic()
                racing = pool.submit(claim, second_sessions, name, NOW, HOUR)
                time.sleep(0.3)
                assert not racing.done()
                held.commit()
                assert racing.result(timeout=10) is None
                assert time.monotonic() - started >= 0.3
    finally:
        with engine.begin() as connection:
            connection.execute(delete(WorkerTask).where(WorkerTask.name == name))
        with Session(engine) as check:
            assert check.scalar(select(WorkerTask).where(WorkerTask.name == name)) is None
