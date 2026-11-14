"""The worker's periodic tasks: claim a due run, and record how it ended.

A run is claimed by one conditional UPDATE that moves ``next_run_at`` a whole interval forward,
committed before any GitHub call. Two workers claiming at once both reach the row; under Read
Committed the second waits for the first, then "The search condition of the command (the WHERE
clause) is re-evaluated to see if the updated version of the row still matches", finds the task no
longer due and claims nothing:
https://www.postgresql.org/docs/18/transaction-iso.html#XACT-READ-COMMITTED
No lock or transaction is held while the task runs, so a worker that dies mid-run simply leaves the
task due again one interval later. An advisory lock would hold a connection, or an idle
transaction, open across every GitHub call instead.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird.models import WorkerTask


@dataclass(frozen=True, slots=True)
class Claim:
    """A claimed run of a task: when it started, and when a run last finished all its work."""

    name: str
    started_at: datetime
    last_success_at: datetime | None


def claim(
    sessions: sessionmaker[Session], name: str, now: datetime, interval: timedelta
) -> Claim | None:
    """Claim the task's run due at ``now``; None when it is not due or another worker has it.

    The conditional UPDATE comes first, so two workers meet on it. A task never run before has no
    row: it is claimed by inserting its row already moved forward, and of two workers inserting at
    once, the second finds the conflict and claims nothing.
    """
    with sessions.begin() as session:
        updated = session.execute(
            update(WorkerTask)
            .where(WorkerTask.name == name, WorkerTask.next_run_at <= now)
            .values(next_run_at=now + interval)
            .returning(WorkerTask.last_success_at)
        ).first()
        if updated is not None:
            return Claim(name, now, updated[0])
        inserted = session.execute(
            insert(WorkerTask)
            .values(name=name, next_run_at=now + interval)
            .on_conflict_do_nothing(index_elements=[WorkerTask.name])
            .returning(WorkerTask.name)
        ).first()
    return None if inserted is None else Claim(name, now, None)


def finish(
    sessions: sessionmaker[Session],
    run: Claim,
    *,
    complete: bool,
    resume_at: datetime | None = None,
) -> None:
    """Record how a claimed run ended.

    A run that finished all its work moves ``last_success_at`` to its start; one that did not
    leaves it. A rate limit puts the next run off until at least ``resume_at``. Both only ever
    move forward (PostgreSQL's GREATEST ignores NULL), so a late finish of an older run changes
    nothing a newer one wrote.
    """
    values = {}
    if complete:
        values["last_success_at"] = func.greatest(WorkerTask.last_success_at, run.started_at)
    if resume_at is not None:
        values["next_run_at"] = func.greatest(WorkerTask.next_run_at, resume_at)
    if not values:
        return
    with sessions.begin() as session:
        session.execute(update(WorkerTask).where(WorkerTask.name == run.name).values(values))
