"""The backfill: store a repository's jobs since its cycle start, from GitHub's runs and jobs.

Webhooks carry each job as it completes, so the backfill fills what they cannot: the jobs of the
current cycle from before a repository came under Burnbird, and those of an event that stayed lost.
The hourly reconciliation queues every repository never backfilled (``queue``); the redelivery
re-queues a repository whose job event was lost, and every repository after a gap longer than
GitHub keeps deliveries (``requeue``). The worker then backfills one queued repository per pass
(``run_next``). A re-queue clears the progress and raises the row's generation: a pass already
running writes nothing more and stops, and the next one starts again at the cycle start. No pass
starts before the account's ``data_deleted_at``.

A pass over one repository:
1. **Lease.** A due row is claimed with ``FOR UPDATE SKIP LOCKED`` and its ``retry_at`` moved 30
   minutes ahead, committed before any GitHub call, so two workers never take the same row and a
   worker that dies leaves it due again after the lease.
2. **Ranges.** From the earlier of the account's two cycle starts (or just after ``done_until``)
   to now, one UTC day at a time, oldest first. ``GET /repos/{account}/{repo}/actions/runs``
   "will return up to 1,000 results for each search when using ... created"
   (https://docs.github.com/en/rest/actions/workflow-runs), so a range whose ``total_count``
   reaches the cap is split in half until each part is under it. Every run listed must be this
   repository's: a name taken again by another repository fails the range.
3. **Jobs.** ``GET .../runs/{id}/jobs?filter=all``, because the default ``latest`` leaves out
   earlier attempts and every attempt is billed
   (https://docs.github.com/en/rest/actions/workflow-jobs). Only completed jobs are stored.
4. **Store.** One transaction per run, through ``events.store_job``, the same upsert the webhook
   uses. Inside it the repository row is locked and must still be stored under the installation:
   one removed or moved while it was backfilled is never brought back. Its row is dropped instead.
5. **Progress.** After each range, ``done_until`` records its end. A pass stops at the first range
   that ends after ``RUNS_PER_PASS`` runs, due again at once, so it stays inside its lease and a
   busy repository still finishes over several passes.
6. **Finish.** The row is deleted and ``repository.backfilled_at`` set, in one transaction.

Limits and errors: when the installation's quota (one whose reset has not passed) falls under 10%
of its limit, the rest is kept for the hourly tasks and the backfill waits for the reset. A rate
limit puts the row off until GitHub allows the next call; any other error (a renamed repository's
redirect among them) logs and backs off from one minute, doubling per error, up to an hour,
counted from the client's clock when it is written. A row is never dropped on an error, since that
would lose jobs. A suspended installation's row waits an hour.
"""

import logging
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Final, cast

from pydantic import ValidationError
from sqlalchemy import CursorResult, Delete, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird.billing.forecast import cycle
from burnbird.github import schemas
from burnbird.github.client import GithubAnswerError, GithubApp, RateLimitedError
from burnbird.github.events import store_job
from burnbird.models import Account, Backfill, Installation, Repository
from burnbird.retention import lock_repositories

logger = logging.getLogger(__name__)

LEASE: Final = timedelta(minutes=30)
# A pass stops at the first range that ends after this many runs, so it stays well inside its
# lease and leaves the worker to its other steps; the next pass goes on from there.
RUNS_PER_PASS: Final = 200
# GitHub's cap on the results of one ``created`` range.
RESULT_CAP: Final = 1000
# The backfill stops while fewer than a tenth of the installation's requests are left.
RESERVE_DIVISOR: Final = 10
BACKOFF_BASE: Final = timedelta(minutes=1)
BACKOFF_CAP: Final = timedelta(hours=1)
SUSPENDED_DELAY: Final = timedelta(hours=1)
_RANGE_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True)
class Claimed:
    """A leased backfill row: the repository, how many errors have put it off, and its generation.

    A pass writes its progress, put-offs and finish only while the row keeps this generation; a
    re-queue raises it, so a pass already running never undoes the re-queue.
    """

    repository_id: int
    attempts: int
    generation: int
    # The lease this pass holds; a pass whose lease ran out and was taken over writes nothing.
    leased_until: datetime


@dataclass(frozen=True, slots=True)
class _Target:
    repository_id: int
    installation_id: int
    account_login: str
    name: str
    suspended: bool
    since: datetime
    done_until: datetime | None
    generation: int
    leased_until: datetime


class _ReserveReachedError(RateLimitedError):
    """The installation's quota is under the reserve; nothing more is sent until its reset."""


class _Run(schemas.Payload):
    id: int
    repository: schemas.RepositoryRef


class _Runs(schemas.Payload):
    total_count: int
    workflow_runs: tuple[_Run, ...]


class _Jobs(schemas.Payload):
    jobs: tuple[dict[str, Any], ...]


def queue(session: Session) -> int:
    """Queue every repository never backfilled that is not queued yet; return how many.

    However a repository was first stored (a sign-in, a job's webhook, the reconciliation), it is
    queued once, and never again after its backfill finished.
    """
    queued = session.execute(
        insert(Backfill)
        .from_select(
            [Backfill.repository_id],
            select(Repository.id).where(
                Repository.backfilled_at.is_(None),
                ~select(Backfill.repository_id)
                .where(Backfill.repository_id == Repository.id)
                .exists(),
            ),
        )
        .returning(Backfill.repository_id)
    )
    return len(queued.all())


def requeue(session: Session, repository_ids: Collection[int]) -> int:
    """Queue stored repositories again from the cycle start; return how many rows that touched.

    A lost webhook event or a gap longer than GitHub keeps deliveries calls it. A repository with a
    row has its progress cleared and its generation raised (its attempts and retry time stay), so a
    pass already running stops and the next one starts again at the cycle start, or at the
    account's data deletion if later. Without a row, it gets one, backfilled before or not. An ID
    no repository has is skipped.
    """
    if not repository_ids:
        return 0
    # Repositories first, in ID order, as Delete my data locks them, so the two never deadlock.
    lock_repositories(session, repository_ids, lock="KEY SHARE")
    touched = session.execute(
        insert(Backfill)
        .from_select(
            [Backfill.repository_id],
            select(Repository.id).where(Repository.id.in_(repository_ids)),
        )
        .on_conflict_do_update(
            index_elements=[Backfill.repository_id],
            set_={"done_until": None, "generation": Backfill.generation + 1},
        )
        .returning(Backfill.repository_id)
    )
    return len(touched.all())


def claim(sessions: sessionmaker[Session], now: datetime) -> Claimed | None:
    """Lease the oldest due row for ``LEASE``; None when none is due or every due one is taken."""
    leased_until = now + LEASE
    with sessions.begin() as session:
        due = (
            select(Backfill.repository_id)
            .where(or_(Backfill.retry_at.is_(None), Backfill.retry_at <= now))
            .order_by(Backfill.retry_at.nulls_first(), Backfill.repository_id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        row = session.execute(
            update(Backfill)
            .where(Backfill.repository_id == due)
            .values(retry_at=leased_until)
            .returning(Backfill.repository_id, Backfill.attempts, Backfill.generation)
        ).first()
    return None if row is None else Claimed(row[0], row[1], row[2], leased_until)


def run_next(sessions: sessionmaker[Session], github: GithubApp, now: datetime) -> bool:
    """Backfill one due repository, at most ``RUNS_PER_PASS`` runs; return whether one was claimed.

    A put-off counts from the client's clock when it is written, not from the start of the pass.
    """
    claimed = claim(sessions, now)
    if claimed is None:
        return False
    try:
        _backfill(sessions, github, claimed, now)
    except RateLimitedError as err:
        logger.warning("backfill: repository %d put off by a rate limit", claimed.repository_id)
        _put_off(sessions, claimed, max(err.resume_at, github.now() + _backoff(claimed.attempts)))
    except Exception:
        # Logged and put off, never dropped: dropping the row would lose the repository's jobs.
        logger.exception("backfill: repository %d failed; put off", claimed.repository_id)
        _put_off(sessions, claimed, github.now() + _backoff(claimed.attempts), failed=True)
    return True


def _backfill(
    sessions: sessionmaker[Session], github: GithubApp, claimed: Claimed, now: datetime
) -> None:
    target = _target(sessions, claimed, now)
    if target is None:
        # The repository went after the claim; its row went with it (ON DELETE CASCADE).
        return
    if target.suspended:
        _put_off(sessions, claimed, github.now() + SUSPENDED_DELAY)
        return
    end = now.astimezone(UTC).replace(microsecond=0)
    start = target.since
    if target.done_until is not None:
        start = max(start, target.done_until + timedelta(seconds=1))
    taken = 0
    for day_start, day_end in _days(start, end):
        for window_end, runs in _windows(github, target, day_start, day_end):
            for run_id in runs:
                _check_reserve(github, target.installation_id)
                jobs = _completed_jobs(github, target, run_id)
                if not _store_run(sessions, target, jobs):
                    return
            _save_progress(sessions, target, window_end)
            taken += len(runs)
            if taken >= RUNS_PER_PASS and window_end < end:
                # The pass has done its share: due again at once, from where it stopped.
                _put_off(sessions, claimed, now)
                return
    _finish(sessions, target, now)


def _finish(sessions: sessionmaker[Session], target: _Target, now: datetime) -> None:
    """Delete the row and mark the repository backfilled, only while this pass still holds it."""
    with sessions.begin() as session:
        finished = _count(
            session,
            delete(Backfill).where(
                *_held(target.repository_id, target.generation, target.leased_until)
            ),
        )
        if not finished:
            # Re-queued or taken over meanwhile: the pass that holds it now does the finish.
            return
        # Only under the installation it was backfilled for: a repository moved meanwhile is
        # still to be backfilled for its new account.
        session.execute(
            update(Repository)
            .where(
                Repository.id == target.repository_id,
                Repository.installation_id == target.installation_id,
            )
            .values(backfilled_at=now)
        )
    logger.info("backfill: repository %d backfilled", target.repository_id)


def _held(repository_id: int, generation: int, leased_until: datetime) -> tuple[Any, ...]:
    """Return the conditions that match the row as this pass claimed it: generation and lease."""
    return (
        Backfill.repository_id == repository_id,
        Backfill.generation == generation,
        Backfill.retry_at == leased_until,
    )


def _target(sessions: sessionmaker[Session], claimed: Claimed, now: datetime) -> _Target | None:
    repository_id = claimed.repository_id
    with sessions.begin() as session:
        row = session.execute(
            select(
                Repository.installation_id,
                Account.login,
                Repository.name,
                Installation.suspended,
                Account.github_cycle_day,
                Account.blacksmith_cycle_day,
                Account.data_deleted_at,
                Backfill.done_until,
            )
            .join(Installation, Installation.id == Repository.installation_id)
            .join(Account, Account.id == Installation.account_id)
            .join(Backfill, Backfill.repository_id == Repository.id)
            .where(Repository.id == repository_id)
        ).first()
    if row is None:
        return None
    installation_id, login, name, suspended, github_day, blacksmith_day, deleted_at, done_until = (
        row
    )
    today = now.astimezone(UTC).date()
    start = min(cycle(today, github_day)[0], cycle(today, blacksmith_day)[0])
    since = datetime.combine(start, time(), UTC)
    if deleted_at is not None:
        # Never before the account's data was deleted: nothing older is fetched or stored again.
        since = max(since, deleted_at.astimezone(UTC).replace(microsecond=0))
    return _Target(
        repository_id,
        installation_id,
        login,
        name,
        suspended,
        since,
        done_until,
        claimed.generation,
        claimed.leased_until,
    )


def _days(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    """Yield ``[start, end]`` as ranges of one UTC day each, oldest first, to the second."""
    day_start = start
    while day_start <= end:
        day_end = min(datetime.combine(day_start.date(), time(23, 59, 59), UTC), end)
        yield day_start, day_end
        day_start = day_end + timedelta(seconds=1)


def _windows(
    github: GithubApp, target: _Target, start: datetime, end: datetime
) -> Iterator[tuple[datetime, list[int]]]:
    """Yield each range's end and its runs' IDs, oldest range first, split at the result cap.

    Every run must be one of this repository's: a name that now belongs to another repository
    (a rename whose old name was taken again) fails the range rather than store its jobs here.
    """
    window = f"{start.strftime(_RANGE_FORMAT)}..{end.strftime(_RANGE_FORMAT)}"
    path = (
        f"repos/{target.account_login}/{target.name}/actions/runs"
        f"?created={window}&exclude_pull_requests=true"
    )
    call = f"GET /repos/{{account}}/{{repo}}/actions/runs of repository {target.repository_id}"
    _check_reserve(github, target.installation_id)
    pages = github.pages(path, target.installation_id)
    try:
        first = _Runs.model_validate(next(pages))
        if first.total_count >= RESULT_CAP:
            middle = (start + (end - start) / 2).replace(microsecond=0)
            if middle <= start or middle >= end:
                raise GithubAnswerError(f"{call} has {RESULT_CAP} runs in one second")
            yield from _windows(github, target, start, middle)
            yield from _windows(github, target, middle + timedelta(seconds=1), end)
            return
        runs = [*first.workflow_runs]
        for page in pages:
            runs.extend(_Runs.model_validate(page).workflow_runs)
    except ValidationError:
        # Pydantic's message would repeat the values (actor logins, commit authors' emails).
        raise GithubAnswerError(f"{call} answered a run not as documented") from None
    if any(run.repository.id != target.repository_id for run in runs):
        raise GithubAnswerError(f"{call} listed a run of another repository")
    yield end, [run.id for run in runs]


def _completed_jobs(github: GithubApp, target: _Target, run_id: int) -> list[schemas.WorkflowJob]:
    path = f"repos/{target.account_login}/{target.name}/actions/runs/{run_id}/jobs?filter=all"
    try:
        return [
            schemas.WorkflowJob.model_validate(job)
            for page in github.pages(path, target.installation_id)
            for job in _Jobs.model_validate(page).jobs
            if job.get("status") == "completed"
        ]
    except ValidationError:
        # Pydantic's message would repeat the values; the call is enough.
        raise GithubAnswerError(f"GET {path} answered a job not as documented") from None


def _store_run(
    sessions: sessionmaker[Session], target: _Target, jobs: list[schemas.WorkflowJob]
) -> bool:
    """Store one run's jobs; False when the pass must stop.

    That is when the repository left the installation meanwhile (its row is dropped), or when its
    row was taken out of the queue (Delete my data).
    """
    with sessions.begin() as session:
        # Locked, so a removal or a move waits until this run's jobs are stored, and cascades.
        stored = session.scalars(
            select(Repository)
            .where(
                Repository.id == target.repository_id,
                Repository.installation_id == target.installation_id,
            )
            .with_for_update()
        ).first()
        if stored is None:
            logger.warning(
                "backfill: repository %d is no longer stored under installation %d; dropped",
                target.repository_id,
                target.installation_id,
            )
            session.execute(delete(Backfill).where(Backfill.repository_id == target.repository_id))
            return False
        # Locked after the repository, in the order Delete my data locks them: once it has taken
        # this repository out of the queue, this pass stops. A later re-queue (a lost event, a
        # gap) stores only jobs created after the deletion: store_job skips the older ones.
        queued = session.execute(
            select(Backfill.generation, Backfill.retry_at)
            .where(Backfill.repository_id == target.repository_id)
            .with_for_update()
        ).first()
        if queued is None:
            logger.info(
                "backfill: repository %d was dequeued while it was backfilled; stopped",
                target.repository_id,
            )
            return False
        if queued != (target.generation, target.leased_until):
            logger.info(
                "backfill: repository %d was re-queued or taken over while it was backfilled; "
                "stopped",
                target.repository_id,
            )
            return False
        repository = schemas.Repository(id=stored.id, name=stored.name, private=stored.private)
        for job in jobs:
            store_job(
                session,
                schemas.WorkflowJobCompleted(
                    installation=schemas.InstallationRef(id=target.installation_id),
                    repository=repository,
                    workflow_job=job,
                ),
            )
    return True


def _check_reserve(github: GithubApp, installation_id: int) -> None:
    quota = github.quota(installation_id)
    # A quota whose reset has passed says nothing; the next answer brings a fresh one.
    if (
        quota is not None
        and quota.reset_at > github.now()
        and quota.remaining * RESERVE_DIVISOR < quota.limit
    ):
        raise _ReserveReachedError("the backfill's reserve", quota.reset_at)


def _save_progress(sessions: sessionmaker[Session], target: _Target, done_until: datetime) -> None:
    with sessions.begin() as session:
        session.execute(
            update(Backfill)
            .where(*_held(target.repository_id, target.generation, target.leased_until))
            # Only forward: a worker whose lease ran out never moves a newer worker's progress
            # back.
            .values(done_until=func.greatest(Backfill.done_until, done_until))
        )


def _count(session: Session, statement: Delete) -> int:
    return cast(CursorResult[Any], session.execute(statement)).rowcount


def _backoff(attempts: int) -> timedelta:
    # The shift is bounded first, so no count of attempts can overflow the wait.
    return min(BACKOFF_BASE * (1 << min(attempts, 12)), BACKOFF_CAP)


def _put_off(
    sessions: sessionmaker[Session], claimed: Claimed, retry_at: datetime, *, failed: bool = False
) -> None:
    values: dict[str, Any] = {"retry_at": retry_at}
    if failed:
        values["attempts"] = claimed.attempts + 1
    with sessions.begin() as session:
        session.execute(
            update(Backfill)
            .where(*_held(claimed.repository_id, claimed.generation, claimed.leased_until))
            .values(values)
        )
