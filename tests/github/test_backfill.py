"""The backfill of a repository's jobs since the cycle start, against a fake api.github.com.

The fake serves invented runs and jobs through the real App client: it filters runs by their
``created`` range and caps a range's results the way GitHub documents, answers the jobs list with
the latest attempt only unless ``filter=all`` is asked, and can answer a renamed repository with a
redirect, a rate limit, or run a hook while a list is fetched. No account is real.
"""

import logging
import secrets
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx2
import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import Engine, delete, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.github import backfill, events, reconcile, redeliver, schemas
from burnbird.github.client import API_HOST, GithubApp
from burnbird.models import Account, Backfill, Installation, Job, Repository
from tests.github.conftest import workflow_job_body

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
API = f"https://{API_HOST}"
ORG, OTHER_ORG = 3001, 3002
INSTALLATION, OTHER_INSTALLATION = 4001, 4002
REPOSITORY = 5001

type Body = dict[str, Any]


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run_item(run_id: int, created_at: str, repository_id: int = REPOSITORY) -> Body:
    """One entry of ``GET /repos/{account}/{repo}/actions/runs``, with fields Burnbird ignores."""
    return {
        "id": run_id,
        "name": "ci",
        "status": "completed",
        "created_at": created_at,
        "run_attempt": 2,
        "repository": {
            "id": repository_id,
            "name": "example-app",
            "full_name": "example-org/example-app",
            "private": True,
        },
    }


def job_item(job_id: int, run_id: int, *, attempt: int = 1, status: str = "completed") -> Body:
    """One entry of ``GET .../runs/{id}/jobs``: the webhook's job object, as the plan notes."""
    job = dict(
        workflow_job_body(job_id=job_id, installation_id=INSTALLATION, repository_id=REPOSITORY)[
            "workflow_job"
        ]
    )
    job.update(run_id=run_id, run_attempt=attempt, status=status)
    if status != "completed":
        job.update(conclusion=None, completed_at=None)
    return job


class FakeGithub:
    """Token requests, the runs list and the jobs list of invented repositories."""

    def __init__(self) -> None:
        # (account, name) -> repository ID; a renamed one answers a redirect to its ID instead.
        self.repositories: dict[tuple[str, str], int] = {
            ("example-org", "example-app"): REPOSITORY
        }
        self.renamed: dict[tuple[str, str], int] = {}
        self.runs: dict[int, list[Body]] = {REPOSITORY: []}
        self.jobs: dict[int, list[Body]] = {}
        # GitHub returns at most this many results for one ``created`` range.
        self.cap = 1000
        self.page_size = 100
        self.quota: tuple[int, int, int] | None = None
        self.hooks: dict[str, Callable[[], None]] = {}
        self.override: Callable[[httpx2.Request], httpx2.Response | None] = lambda _: None
        self.tokens: dict[str, int] = {}
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if (answer := self.override(request)) is not None:
            return answer
        path = request.url.path
        if request.method == "POST":
            token = "gh" + "s_" + secrets.token_urlsafe(30)
            self.tokens[token] = int(path.split("/")[3])
            expires = (NOW + timedelta(hours=1)).isoformat()
            return httpx2.Response(201, json={"token": token, "expires_at": expires})
        parts = path.split("/")
        account, name = parts[2], parts[3]
        if (account, name) in self.renamed:
            location = (
                f"{API}/repositories/{self.renamed[(account, name)]}{path.split(name, 1)[1]}"
            )
            return httpx2.Response(301, headers={"location": location})
        repository_id = self.repositories.get((account, name))
        if repository_id is None:
            return httpx2.Response(404, json={"message": "Not Found"})
        headers = self._quota_headers()
        if len(parts) == 6:
            return self._runs(request, repository_id, headers)
        run_id = int(parts[6])
        if (hook := self.hooks.pop(f"jobs:{run_id}", None)) is not None:
            hook()
        jobs = self.jobs.get(run_id, [])
        if request.url.params.get("filter") != "all":
            latest = max((job["run_attempt"] for job in jobs), default=0)
            jobs = [job for job in jobs if job["run_attempt"] == latest]
        return self._page(request, "jobs", jobs, len(jobs), headers)

    def _runs(
        self, request: httpx2.Request, repository_id: int, headers: dict[str, str]
    ) -> httpx2.Response:
        if (hook := self.hooks.pop("runs", None)) is not None:
            hook()
        start, end = (_when(value) for value in request.url.params["created"].split(".."))
        matching = [
            run for run in self.runs[repository_id] if start <= _when(run["created_at"]) <= end
        ]
        # The cap: only the first 1,000 results of a range, and a count that says so.
        shown = matching[: self.cap]
        return self._page(request, "workflow_runs", shown, len(shown), headers)

    def _page(
        self,
        request: httpx2.Request,
        key: str,
        items: list[Body],
        total: int,
        headers: dict[str, str],
    ) -> httpx2.Response:
        page = int(request.url.params.get("page", "1"))
        chunk = items[(page - 1) * self.page_size : page * self.page_size]
        if page * self.page_size < len(items):
            query = dict(request.url.params)
            query["page"] = str(page + 1)
            following = httpx2.URL(f"{API}{request.url.path}", params=query)
            headers = {**headers, "Link": f'<{following}>; rel="next"'}
        return httpx2.Response(200, headers=headers, json={"total_count": total, key: chunk})

    def _quota_headers(self) -> dict[str, str]:
        if self.quota is None:
            return {}
        remaining, limit, reset = self.quota
        return {
            "x-ratelimit-remaining": str(remaining),
            "x-ratelimit-limit": str(limit),
            "x-ratelimit-reset": str(reset),
        }

    def run_requests(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path.endswith("/actions/runs")]

    def job_requests(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path.endswith("/jobs")]


@pytest.fixture
def github() -> FakeGithub:
    return FakeGithub()


@pytest.fixture
def clock() -> list[datetime]:
    """The client's time of day; a test moves it by replacing the one item."""
    return [NOW]


@pytest.fixture
def app(rsa_key: RSAKey, github: FakeGithub, clock: list[datetime]) -> Iterator[GithubApp]:
    ticks = iter(range(10**6))
    app = GithubApp(
        "Iv23.example",
        rsa_key,
        transport=httpx2.MockTransport(lambda request: github.handle(request)),
        clock=lambda: clock[0],
        monotonic=lambda: float(next(ticks)),
        sleep=lambda _: None,
    )
    yield app
    app.close()


@pytest.fixture
def world(session: Session) -> Session:
    """An organisation with one installation and one repository queued for backfill."""
    for account_id, login, installation_id, repository_id, name in (
        (ORG, "example-org", INSTALLATION, REPOSITORY, "example-app"),
        (OTHER_ORG, "example-other-org", OTHER_INSTALLATION, 5002, "other-app"),
    ):
        session.add(
            Account(
                id=account_id,
                login=login,
                type="Organization",
                github_plan="GitHub Free for organizations",
                github_cycle_day=13,
                blacksmith_cycle_day=10,
            )
        )
        session.flush()
        session.add(Installation(id=installation_id, account_id=account_id, created_at=NOW))
        session.flush()
        session.add(
            Repository(id=repository_id, installation_id=installation_id, name=name, private=True)
        )
    session.flush()
    session.add(Backfill(repository_id=REPOSITORY))
    session.flush()
    return session


def _run(sessions: sessionmaker[Session], app: GithubApp, now: datetime = NOW) -> bool:
    return backfill.run_next(sessions, app, now)


def _row(session: Session) -> Backfill | None:
    session.expire_all()
    return session.get(Backfill, REPOSITORY)


def _jobs(session: Session) -> set[tuple[int, int]]:
    session.expire_all()
    return {(job.id, job.run_attempt) for job in session.scalars(select(Job))}


def _backfilled_at(session: Session, repository_id: int = REPOSITORY) -> datetime | None:
    session.expire_all()
    return session.scalar(select(Repository.backfilled_at).where(Repository.id == repository_id))


# Queueing, by the hourly reconciliation.


def test_every_repository_never_backfilled_is_queued_however_it_was_stored(
    world: Session,
) -> None:
    # 5002 was stored by a sign-in or a webhook, never by a reconciliation; 5001 is queued.
    events.store_repository(
        world, INSTALLATION, schemas.Repository(id=5003, name="from-a-job", private=True)
    )
    assert backfill.queue(world) == 2
    world.expire_all()
    assert set(world.scalars(select(Backfill.repository_id))) == {REPOSITORY, 5002, 5003}


def test_a_backfilled_repository_is_not_queued_again(world: Session) -> None:
    world.execute(delete(Backfill))
    world.get_one(Repository, REPOSITORY).backfilled_at = NOW
    world.flush()
    assert backfill.queue(world) == 1
    world.expire_all()
    assert set(world.scalars(select(Backfill.repository_id))) == {5002}


def test_a_queued_row_keeps_its_attempts_and_retry_time(world: Session) -> None:
    row = world.get_one(Backfill, REPOSITORY)
    row.attempts, row.retry_at = 3, NOW + timedelta(minutes=8)
    world.flush()
    backfill.queue(world)
    world.expire_all()
    row = world.get_one(Backfill, REPOSITORY)
    assert (row.attempts, row.retry_at) == (3, NOW + timedelta(minutes=8))


def test_the_reconciliation_queues_after_reconciling(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: list[bool] = []

    def listed(_: GithubApp) -> reconcile._Listed:
        return reconcile._Listed(frozenset({INSTALLATION, OTHER_INSTALLATION}), (), 0)

    def queue(_: Session) -> int:
        queued.append(True)
        return 0

    monkeypatch.setattr(reconcile, "_list_installations", listed)
    monkeypatch.setattr(backfill, "queue", queue)
    reconcile.reconcile(sessions, app, NOW)
    assert queued == [True]


# The backfill of one repository.


def test_runs_since_the_cycle_start_are_listed_and_every_attempt_is_stored(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.runs[REPOSITORY] = [
        run_item(701, "2026-11-12T09:00:00Z"),
        # Before the earlier of the two cycle starts (Blacksmith's day 10): not listed.
        run_item(702, "2026-11-09T23:59:59Z"),
    ]
    github.jobs = {
        701: [job_item(9001, 701, attempt=1), job_item(9002, 701, attempt=2)],
        702: [job_item(9003, 702)],
    }
    assert _run(sessions, app) is True
    # One UTC day per range, oldest first, from the earlier cycle start to now.
    assert [r.url.params["created"] for r in github.run_requests()] == [
        "2026-11-10T00:00:00Z..2026-11-10T23:59:59Z",
        "2026-11-11T00:00:00Z..2026-11-11T23:59:59Z",
        "2026-11-12T00:00:00Z..2026-11-12T23:59:59Z",
        "2026-11-13T00:00:00Z..2026-11-13T23:59:59Z",
        "2026-11-14T00:00:00Z..2026-11-14T12:00:00Z",
    ]
    assert all(r.url.params["exclude_pull_requests"] == "true" for r in github.run_requests())
    assert all(r.url.params["filter"] == "all" for r in github.job_requests())
    assert _jobs(world) == {(9001, 1), (9002, 2)}


def test_only_completed_jobs_are_stored(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.runs[REPOSITORY] = [run_item(701, "2026-11-12T09:00:00Z")]
    github.jobs = {
        701: [
            job_item(9001, 701),
            job_item(9002, 701, status="in_progress"),
            job_item(9003, 701, status="queued"),
        ]
    }
    assert _run(sessions, app) is True
    assert _jobs(world) == {(9001, 1)}


def test_a_backfilled_job_is_the_row_the_webhook_writes(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    def columns(session: Session) -> dict[str, Any]:
        session.expire_all()
        job = session.get_one(Job, (9001, 1))
        return {c.name: getattr(job, c.name) for c in Job.__table__.columns}

    # The webhook body's run is the job's ID times ten.
    github.runs[REPOSITORY] = [run_item(90010, "2026-11-12T09:00:00Z")]
    github.jobs = {90010: [job_item(9001, 90010)]}
    assert _run(sessions, app) is True
    backfilled = columns(world)
    world.execute(delete(Job))
    body = workflow_job_body(job_id=9001, installation_id=INSTALLATION, repository_id=REPOSITORY)
    events.HANDLERS[("workflow_job", "completed")](world, body)
    assert columns(world) == backfilled


def test_a_range_at_the_result_cap_is_split_until_every_run_is_listed(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cap of 3 stands for GitHub's 1,000: five runs in the cycle need two ranges or more.
    monkeypatch.setattr(backfill, "RESULT_CAP", 3)
    github.cap = 3
    # Five runs on one day; the day's range is split at its middle, 11:59:59, and the second
    # half starts a second later, so runs at both edges are listed once each.
    times = ["03:00:00", "11:59:59", "12:00:00", "18:00:00", "23:59:59"]
    github.runs[REPOSITORY] = [
        run_item(700 + n, f"2026-11-12T{when}Z") for n, when in enumerate(times)
    ]
    github.jobs = {700 + n: [job_item(9000 + n, 700 + n)] for n in range(5)}
    assert _run(sessions, app) is True
    assert _jobs(world) == {(9000 + n, 1) for n in range(5)}
    ranges = [r.url.params["created"] for r in github.run_requests()]
    assert "2026-11-12T00:00:00Z..2026-11-12T11:59:59Z" in ranges
    assert "2026-11-12T12:00:00Z..2026-11-12T23:59:59Z" in ranges


def test_every_page_of_runs_and_jobs_is_read(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.page_size = 2
    github.runs[REPOSITORY] = [run_item(700 + n, "2026-11-12T09:00:00Z") for n in range(5)]
    github.jobs = {700: [job_item(9000 + n, 700) for n in range(5)]}
    assert _run(sessions, app) is True
    assert _jobs(world) == {(9000 + n, 1) for n in range(5)}


def test_finishing_deletes_the_row_and_marks_the_repository_backfilled(
    sessions: sessionmaker[Session], world: Session, app: GithubApp
) -> None:
    assert _run(sessions, app) is True
    assert _row(world) is None
    assert _backfilled_at(world) == NOW


def test_nothing_due_claims_nothing(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    world.get_one(Backfill, REPOSITORY).retry_at = NOW + timedelta(seconds=1)
    world.flush()
    assert _run(sessions, app) is False
    assert github.requests == []


def test_a_claimed_row_is_leased_for_thirty_minutes(
    sessions: sessionmaker[Session], world: Session
) -> None:
    assert backfill.claim(sessions, NOW) == backfill.Claimed(
        REPOSITORY, 0, 0, NOW + backfill.LEASE
    )
    row = _row(world)
    assert row is not None
    assert row.retry_at == NOW + timedelta(minutes=30)
    assert backfill.claim(sessions, NOW + timedelta(minutes=29)) is None
    assert backfill.claim(sessions, NOW + timedelta(minutes=30)) == backfill.Claimed(
        REPOSITORY, 0, 0, NOW + timedelta(minutes=30) + backfill.LEASE
    )


# Limits and errors.


def test_a_rate_limit_mid_repository_keeps_the_row_and_the_rerun_is_idempotent(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    clock: list[datetime],
) -> None:
    github.runs[REPOSITORY] = [
        run_item(701, "2026-11-12T09:00:00Z"),
        run_item(702, "2026-11-12T10:00:00Z"),
    ]
    github.jobs = {701: [job_item(9001, 701)], 702: [job_item(9002, 702)]}
    limited = {"done": False}

    def once(request: httpx2.Request) -> httpx2.Response | None:
        if request.url.path.endswith("/702/jobs") and not limited["done"]:
            limited["done"] = True
            return httpx2.Response(429, headers={"retry-after": "600"})
        return None

    github.override = once
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    assert (row.retry_at, row.attempts) == (NOW + timedelta(minutes=10), 0)
    assert _jobs(world) == {(9001, 1)}
    assert _backfilled_at(world) is None
    later = NOW + timedelta(minutes=10)
    clock[0] = later
    assert _run(sessions, app, later) is True
    assert _jobs(world) == {(9001, 1), (9002, 1)}
    assert _row(world) is None


def test_the_reserve_puts_the_backfill_off_until_the_reset(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    reset = int((NOW + timedelta(minutes=20)).timestamp())
    # 499 of 5,000 left is under 10%: the rest is kept for the hourly tasks.
    github.quota = (499, 5000, reset)
    github.runs[REPOSITORY] = [run_item(701, "2026-11-12T09:00:00Z")]
    github.jobs = {701: [job_item(9001, 701)]}
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    assert row.retry_at == datetime.fromtimestamp(reset, UTC)
    assert github.job_requests() == []
    assert _jobs(world) == set()


def test_ten_percent_left_is_not_under_the_reserve(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.quota = (500, 5000, int((NOW + timedelta(minutes=20)).timestamp()))
    github.runs[REPOSITORY] = [run_item(701, "2026-11-12T09:00:00Z")]
    github.jobs = {701: [job_item(9001, 701)]}
    assert _run(sessions, app) is True
    assert _jobs(world) == {(9001, 1)}
    assert _row(world) is None


@pytest.mark.parametrize(
    ("attempts", "wait"), [(0, 60), (1, 120), (5, 1920), (6, 3600), (20, 3600)]
)
def test_an_error_keeps_the_row_and_backs_off_up_to_an_hour(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    attempts: int,
    wait: int,
) -> None:
    world.get_one(Backfill, REPOSITORY).attempts = attempts
    world.flush()
    github.override = lambda request: httpx2.Response(500) if request.method == "GET" else None
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    assert (row.attempts, row.retry_at) == (attempts + 1, NOW + timedelta(seconds=wait))


def test_a_renamed_repository_s_error_keeps_the_row_for_the_next_attempt(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    # GitHub answers the old name with a redirect; the client never follows one with a token.
    github.renamed = {("example-org", "example-app"): REPOSITORY}
    github.repositories = {("example-org", "renamed-app"): REPOSITORY}
    github.runs[REPOSITORY] = [run_item(701, "2026-11-12T09:00:00Z")]
    github.jobs = {701: [job_item(9001, 701)]}
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    assert (row.attempts, row.retry_at) == (1, NOW + timedelta(seconds=60))
    assert all(r.url.path.startswith("/repos/") for r in github.requests if r.method == "GET")
    # The hourly reconciliation stores the new name; the next attempt uses it.
    world.get_one(Repository, REPOSITORY).name = "renamed-app"
    world.flush()
    assert _run(sessions, app, NOW + timedelta(seconds=60)) is True
    assert _jobs(world) == {(9001, 1)}
    assert _row(world) is None


def test_a_suspended_installation_is_put_off_an_hour(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    world.get_one(Installation, INSTALLATION).suspended = True
    world.flush()
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    assert (row.attempts, row.retry_at) == (0, NOW + timedelta(hours=1))
    assert github.requests == []


# Rows that change while they are backfilled.


def test_a_repository_removed_during_its_backfill_is_not_brought_back(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github.runs[REPOSITORY] = [run_item(701, "2026-11-12T09:00:00Z")]
    github.jobs = {701: [job_item(9001, 701)]}

    # A webhook removes the repository while its jobs are listed.
    def removed() -> None:
        retention.remove_repositories(world, INSTALLATION, [REPOSITORY])

    github.hooks["jobs:701"] = removed
    assert _run(sessions, app) is True
    world.expire_all()
    assert world.get(Repository, REPOSITORY) is None
    assert _jobs(world) == set()
    assert _row(world) is None
    assert "repository 5001 is no longer stored under installation 4001" in caplog.text


def test_a_repository_moved_during_its_backfill_stays_with_its_new_installation(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github.runs[REPOSITORY] = [run_item(701, "2026-11-12T09:00:00Z")]
    github.jobs = {701: [job_item(9001, 701)]}

    def moved() -> None:
        world.get_one(Repository, REPOSITORY).installation_id = OTHER_INSTALLATION
        world.flush()

    github.hooks["jobs:701"] = moved
    assert _run(sessions, app) is True
    world.expire_all()
    assert world.get_one(Repository, REPOSITORY).installation_id == OTHER_INSTALLATION
    assert _jobs(world) == set()
    assert _row(world) is None
    assert "no longer stored under installation 4001" in caplog.text


def test_a_removed_repository_takes_its_queued_row_with_it(world: Session) -> None:
    retention.remove_repositories(world, INSTALLATION, [REPOSITORY])
    assert _row(world) is None


def test_two_workers_never_take_the_same_row(engine: Engine) -> None:
    # Two connections need committed rows; they are deleted again at the end.
    with engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=3901,
                login="example-race",
                type="Organization",
                github_plan="GitHub Free for organizations",
            )
        )
        connection.execute(insert(Installation).values(id=4901, account_id=3901, created_at=NOW))
        connection.execute(
            insert(Repository).values(id=5901, installation_id=4901, name="race", private=True)
        )
        connection.execute(insert(Backfill).values(repository_id=5901))
    try:
        with engine.connect() as first, engine.connect() as second:
            held = first.begin()
            first_sessions = sessionmaker(bind=first, join_transaction_mode="create_savepoint")
            assert backfill.claim(first_sessions, NOW) == backfill.Claimed(
                5901, 0, 0, NOW + backfill.LEASE
            )
            # Without SKIP LOCKED this would wait for the lock and fail after a second.
            second.execute(text("SET lock_timeout = '1s'"))
            second.commit()
            assert backfill.claim(sessionmaker(bind=second), NOW) is None
            held.commit()
            # Committed, the lease keeps it from the second worker too.
            assert backfill.claim(sessionmaker(bind=second), NOW) is None
    finally:
        with engine.begin() as connection:
            connection.execute(delete(Account).where(Account.id == 3901))


def test_the_worker_backfills_one_repository_per_pass_after_its_tasks(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    from burnbird.worker import run_once

    world.add(Backfill(repository_id=5002))
    world.execute(delete(Backfill).where(Backfill.repository_id == REPOSITORY))
    world.add(Backfill(repository_id=REPOSITORY))
    world.flush()
    github.repositories[("example-other-org", "other-app")] = 5002
    github.runs[5002] = []
    github.override = lambda request: (
        httpx2.Response(200, json=[]) if request.url.path == "/app/installations" else None
    )
    run_once(sessions, app, NOW)
    world.expire_all()
    assert len(list(world.scalars(select(Backfill)))) == 1


# From the independent review.


def test_runs_of_another_repository_under_the_same_name_are_never_stored(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    # 5001 was renamed and a new repository took its old name before the reconciliation ran.
    github.repositories = {("example-org", "example-app"): 5009}
    github.runs[5009] = [run_item(701, "2026-11-12T09:00:00Z", repository_id=5009)]
    github.jobs = {701: [job_item(9001, 701)]}
    assert _run(sessions, app) is True
    assert _jobs(world) == set()
    row = _row(world)
    assert row is not None
    assert row.attempts == 1
    assert github.job_requests() == []


def test_progress_is_saved_per_range_and_a_pass_stops_at_its_budget(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backfill, "RUNS_PER_PASS", 2)
    github.runs[REPOSITORY] = [
        run_item(701, "2026-11-10T09:00:00Z"),
        run_item(702, "2026-11-11T09:00:00Z"),
        run_item(703, "2026-11-12T09:00:00Z"),
    ]
    github.jobs = {
        701: [job_item(9001, 701)],
        702: [job_item(9002, 702)],
        703: [job_item(9003, 703)],
    }
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    # Two runs stored, the second day done: due again at once, from where it stopped.
    assert (row.done_until, row.retry_at, row.attempts) == (
        datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC),
        NOW,
        0,
    )
    assert _jobs(world) == {(9001, 1), (9002, 1)}
    assert _backfilled_at(world) is None
    github.requests.clear()
    assert _run(sessions, app) is True
    assert github.run_requests()[0].url.params["created"].startswith("2026-11-12T00:00:00Z..")
    assert _jobs(world) == {(9001, 1), (9002, 1), (9003, 1)}
    assert _row(world) is None
    assert _backfilled_at(world) == NOW


def test_after_an_error_the_backfill_resumes_after_the_last_range_it_finished(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.runs[REPOSITORY] = [
        run_item(701, "2026-11-10T09:00:00Z"),
        run_item(703, "2026-11-12T09:00:00Z"),
    ]
    github.jobs = {701: [job_item(9001, 701)], 703: [job_item(9003, 703)]}
    github.override = lambda request: (
        httpx2.Response(500) if request.url.path.endswith("/703/jobs") else None
    )
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    assert (row.done_until, row.attempts) == (datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC), 1)
    github.override = lambda _: None
    github.requests.clear()
    assert _run(sessions, app, NOW + timedelta(minutes=1)) is True
    assert github.run_requests()[0].url.params["created"].startswith("2026-11-12T00:00:00Z..")
    assert _jobs(world) == {(9001, 1), (9003, 1)}


def test_a_quota_whose_reset_has_passed_does_not_hold_the_backfill(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    clock: list[datetime],
) -> None:
    reset = int((NOW + timedelta(minutes=20)).timestamp())
    github.quota = (10, 5000, reset)
    github.runs[REPOSITORY] = [run_item(701, "2026-11-12T09:00:00Z")]
    github.jobs = {701: [job_item(9001, 701)]}
    assert _run(sessions, app) is True
    assert _jobs(world) == set()
    # After the reset the old answer says nothing; the next call brings a fresh quota.
    github.quota = None
    later = NOW + timedelta(minutes=21)
    clock[0] = later
    assert _run(sessions, app, later) is True
    assert _jobs(world) == {(9001, 1)}


def test_a_put_off_counts_from_the_clock_when_it_is_written(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    clock: list[datetime],
) -> None:
    github.runs[REPOSITORY] = [run_item(701, "2026-11-12T09:00:00Z")]

    def slow(request: httpx2.Request) -> httpx2.Response | None:
        if request.url.path.endswith("/701/jobs"):
            # The pass has run for 25 minutes by now.
            clock[0] = NOW + timedelta(minutes=25)
            return httpx2.Response(500)
        return None

    github.override = slow
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    assert row.retry_at == NOW + timedelta(minutes=26)


def test_a_runs_answer_not_as_documented_never_reaches_the_log_with_its_values(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    planted = "planted-" + secrets.token_hex(6)
    github.override = lambda request: (
        httpx2.Response(200, json={"total_count": planted, "workflow_runs": []})
        if request.url.path.endswith("/actions/runs")
        else None
    )
    assert _run(sessions, app) is True
    assert planted not in caplog.text
    row = _row(world)
    assert row is not None
    assert row.attempts == 1


def test_a_repository_moved_before_the_finish_is_not_marked_backfilled(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    def moved() -> None:
        world.get_one(Repository, REPOSITORY).installation_id = OTHER_INSTALLATION
        world.flush()

    # It moves while the last day is listed, and that day has no run.
    github.hooks["runs"] = moved
    assert _run(sessions, app) is True
    assert _backfilled_at(world) is None
    assert _row(world) is None


def test_a_range_is_written_in_utc_whatever_the_zone_of_now(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    local = NOW.astimezone(timezone(timedelta(hours=2)))
    assert _run(sessions, app, local) is True
    assert github.run_requests()[-1].url.params["created"].endswith("..2026-11-14T12:00:00Z")


def test_a_repository_moved_by_another_transaction_waits_for_it_and_stays_moved(
    engine: Engine, rsa_key: RSAKey, github: FakeGithub
) -> None:
    # Two connections need committed rows; they are deleted again at the end. One connection
    # moves the repository and holds its lock; the backfill on the other waits for it, then
    # finds the repository gone from its installation and brings nothing back.
    with engine.begin() as connection:
        for account_id, installation_id in ((3911, 4911), (3912, 4912)):
            connection.execute(
                insert(Account).values(
                    id=account_id,
                    login=f"example-{account_id}",
                    type="Organization",
                    github_plan="GitHub Free for organizations",
                )
            )
            connection.execute(
                insert(Installation).values(
                    id=installation_id, account_id=account_id, created_at=NOW
                )
            )
        connection.execute(
            insert(Repository).values(id=5911, installation_id=4911, name="locked", private=True)
        )
        connection.execute(insert(Backfill).values(repository_id=5911))
    github.repositories = {("example-3911", "locked"): 5911}
    github.runs[5911] = [run_item(791, "2026-11-12T09:00:00Z", repository_id=5911)]
    github.jobs = {791: [job_item(9911, 791)]}
    ticks = iter(range(10**6))
    app = GithubApp(
        "Iv23.example",
        rsa_key,
        transport=httpx2.MockTransport(github.handle),
        clock=lambda: NOW,
        monotonic=lambda: float(next(ticks)),
        sleep=lambda _: None,
    )
    try:
        with engine.connect() as mover, ThreadPoolExecutor(max_workers=1) as pool:
            held = mover.begin()
            mover.execute(text("UPDATE repository SET installation_id = 4912 WHERE id = 5911"))
            running = pool.submit(backfill.run_next, sessionmaker(bind=engine), app, NOW)
            time.sleep(0.5)
            assert not running.done()
            held.commit()
            assert running.result(timeout=10) is True
        with Session(engine) as check:
            assert check.get_one(Repository, 5911).installation_id == 4912
            assert check.scalar(select(Job).where(Job.id == 9911)) is None
            assert check.get(Backfill, 5911) is None
    finally:
        app.close()
        with engine.begin() as connection:
            connection.execute(delete(Account).where(Account.id.in_([3911, 3912])))


# Delete my data, and progress that only moves forward.

PERSON, PERSONAL_INSTALLATION = 7001, 4007


@pytest.fixture
def personal(world: Session) -> Session:
    """A personal account's installation with two repositories: one queued, one never queued."""
    world.add(
        Account(
            id=PERSON,
            login="example-person",
            type="User",
            github_plan="GitHub Free",
            github_cycle_day=13,
            blacksmith_cycle_day=10,
        )
    )
    world.flush()
    world.add(Installation(id=PERSONAL_INSTALLATION, account_id=PERSON, created_at=NOW))
    world.flush()
    for repository_id, name in ((5071, "personal-app"), (5072, "never-queued")):
        world.add(
            Repository(
                id=repository_id, installation_id=PERSONAL_INSTALLATION, name=name, private=True
            )
        )
    world.flush()
    world.add(Backfill(repository_id=5071))
    # Leave only the personal row due, so the pass takes it.
    world.get_one(Backfill, REPOSITORY).retry_at = NOW + timedelta(hours=1)
    world.flush()
    return world


def test_delete_my_data_during_a_backfill_brings_no_job_back_and_stops_the_pass(
    sessions: sessionmaker[Session],
    personal: Session,
    app: GithubApp,
    github: FakeGithub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    github.repositories = {("example-person", "personal-app"): 5071}
    github.runs[5071] = [
        run_item(701, "2026-11-12T09:00:00Z", repository_id=5071),
        run_item(702, "2026-11-12T10:00:00Z", repository_id=5071),
        run_item(703, "2026-11-13T09:00:00Z", repository_id=5071),
    ]
    github.jobs = {
        701: [job_item(9001, 701)],
        702: [job_item(9002, 702)],
        703: [job_item(9003, 703)],
    }
    # The person asks to delete their data while the backfill lists run 702's jobs.
    github.hooks["jobs:702"] = lambda: _deleted_mine(personal)
    assert _run(sessions, app) is True
    personal.expire_all()
    assert _jobs(personal) == set()
    assert personal.get(Backfill, 5071) is None
    assert github.job_requests()[-1].url.path.endswith("/702/jobs")
    assert "backfill: repository 5071 was dequeued while it was backfilled; stopped" in caplog.text


def _deleted_mine(session: Session) -> None:
    retention.delete_my_data(session, PERSON)


def test_delete_my_data_dequeues_the_account_and_nothing_queues_it_again(
    personal: Session,
) -> None:
    deleted = retention.delete_my_data(personal, PERSON)
    assert deleted.backfills == 1
    personal.expire_all()
    assert personal.get(Backfill, 5071) is None
    # The never-queued repository is marked too, so no reconciliation queues either again; only
    # the organisation's repository 5002, never queued, is queued now.
    assert backfill.queue(personal) == 1
    assert {r.id for r in personal.scalars(select(Repository)) if r.backfilled_at is None} == {
        REPOSITORY,
        5002,
    }
    assert set(personal.scalars(select(Backfill.repository_id))) == {REPOSITORY, 5002}


def test_delete_my_data_leaves_an_organisation_s_backfill_alone(personal: Session) -> None:
    row = personal.get_one(Backfill, REPOSITORY)
    row.attempts, row.done_until = 2, datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC)
    personal.flush()
    retention.delete_my_data(personal, PERSON)
    personal.expire_all()
    row = personal.get_one(Backfill, REPOSITORY)
    assert (row.attempts, row.done_until) == (2, datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC))
    assert _backfilled_at(personal) is None


def test_delete_my_data_waits_for_a_run_that_holds_the_repository(engine: Engine) -> None:
    # Two connections need committed rows; they are deleted again at the end. One connection
    # stands for a backfill run between its two locks: it holds the repository, Delete my data
    # starts, and only then does the run lock the queued row and store a job. Taking the rows in
    # the run's order, Delete my data waits for it instead of deadlocking, then deletes that job.
    with engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=7901, login="example-waiting", type="User", github_plan="GitHub Free"
            )
        )
        connection.execute(insert(Installation).values(id=4921, account_id=7901, created_at=NOW))
        connection.execute(
            insert(Repository).values(id=5921, installation_id=4921, name="held", private=True)
        )
        connection.execute(insert(Backfill).values(repository_id=5921))
    try:
        with engine.connect() as run, ThreadPoolExecutor(max_workers=1) as pool:
            held = run.begin()
            run.execute(text("SET LOCAL lock_timeout = '3s'"))
            run.execute(text("SELECT id FROM repository WHERE id = 5921 FOR UPDATE"))

            def delete_mine() -> None:
                with sessionmaker(bind=engine).begin() as session:
                    session.execute(text("SET LOCAL lock_timeout = '3s'"))
                    retention.delete_my_data(session, 7901)

            deleting = pool.submit(delete_mine)
            time.sleep(0.5)
            assert not deleting.done()
            run.execute(
                text("SELECT repository_id FROM backfill WHERE repository_id = 5921 FOR UPDATE")
            )
            run.execute(
                text(
                    "INSERT INTO job (id, run_attempt, run_id, repository_id, labels, created_at,"
                    " provider) VALUES (9921, 1, 99210, 5921, ARRAY['ubuntu-latest'], now(),"
                    " 'github')"
                )
            )
            held.commit()
            deleting.result(timeout=10)
        with Session(engine) as check:
            assert check.get(Job, (9921, 1)) is None
            assert check.get(Backfill, 5921) is None
            assert check.get_one(Repository, 5921).backfilled_at is not None
    finally:
        with engine.begin() as connection:
            connection.execute(delete(Account).where(Account.id == 7901))


def test_progress_never_moves_back(world: Session, sessions: sessionmaker[Session]) -> None:
    later = datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC)
    world.get_one(Backfill, REPOSITORY).done_until = later
    world.flush()
    claimed = backfill.claim(sessions, NOW)
    assert claimed is not None
    target = backfill._target(sessions, claimed, NOW)
    assert target is not None
    # A worker whose lease ran out finishes an older range after a newer worker moved on.
    backfill._save_progress(sessions, target, datetime(2026, 11, 10, 23, 59, 59, tzinfo=UTC))
    assert _row(world) is not None
    row = _row(world)
    assert row is not None
    assert row.done_until == later


def test_a_row_dequeued_by_another_transaction_mid_run_stores_nothing(
    engine: Engine, rsa_key: RSAKey, github: FakeGithub
) -> None:
    # Two connections need committed rows; they are deleted again at the end. After the claim,
    # another connection takes the row out of the queue and holds it uncommitted; the run must
    # wait for it on the row's lock, then store nothing.
    with engine.begin() as connection:
        connection.execute(
            # Its cycle starts the day of its one run, so the run comes before any range is saved.
            insert(Account).values(
                id=3931,
                login="example-3931",
                type="Organization",
                github_plan="GitHub Free for organizations",
                github_cycle_day=12,
                blacksmith_cycle_day=12,
            )
        )
        connection.execute(insert(Installation).values(id=4931, account_id=3931, created_at=NOW))
        connection.execute(
            insert(Repository).values(id=5931, installation_id=4931, name="dequeued", private=True)
        )
        connection.execute(insert(Backfill).values(repository_id=5931))
    github.repositories = {("example-3931", "dequeued"): 5931}
    github.runs[5931] = [run_item(793, "2026-11-12T09:00:00Z", repository_id=5931)]
    github.jobs = {793: [job_item(9931, 793)]}
    ticks = iter(range(10**6))
    app = GithubApp(
        "Iv23.example",
        rsa_key,
        transport=httpx2.MockTransport(github.handle),
        clock=lambda: NOW,
        monotonic=lambda: float(next(ticks)),
        sleep=lambda _: None,
    )
    sessions = sessionmaker(bind=engine)
    try:
        claimed = backfill.claim(sessions, NOW)
        assert claimed == backfill.Claimed(5931, 0, 0, NOW + backfill.LEASE)
        with engine.connect() as other, ThreadPoolExecutor(max_workers=1) as pool:
            held = other.begin()
            other.execute(text("DELETE FROM backfill WHERE repository_id = 5931"))
            running = pool.submit(backfill._backfill, sessions, app, claimed, NOW)
            time.sleep(0.5)
            assert not running.done()
            held.commit()
            running.result(timeout=10)
        with Session(engine) as check:
            assert check.get(Job, (9931, 1)) is None
            assert check.get_one(Repository, 5931).backfilled_at is None
    finally:
        app.close()
        with engine.begin() as connection:
            connection.execute(delete(Account).where(Account.id == 3931))


# Re-queueing (lost events and the gap rule) and the generation that keeps it from being undone.


def _first_range(github: FakeGithub) -> str:
    return github.run_requests()[0].url.params["created"].split("..")[0]


def test_a_requeue_clears_progress_and_keeps_the_rest(world: Session) -> None:
    row = world.get_one(Backfill, REPOSITORY)
    row.done_until = datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC)
    row.attempts, row.retry_at = 2, NOW + timedelta(minutes=8)
    world.flush()
    assert backfill.requeue(world, [REPOSITORY]) == 1
    world.expire_all()
    row = world.get_one(Backfill, REPOSITORY)
    assert (row.done_until, row.generation, row.attempts, row.retry_at) == (
        None,
        1,
        2,
        NOW + timedelta(minutes=8),
    )


def test_a_requeue_queues_a_backfilled_repository_and_never_an_unknown_one(
    world: Session,
) -> None:
    world.execute(delete(Backfill))
    world.get_one(Repository, REPOSITORY).backfilled_at = NOW - timedelta(days=2)
    world.flush()
    assert backfill.requeue(world, [REPOSITORY, 5999]) == 1
    world.expire_all()
    [row] = list(world.scalars(select(Backfill)))
    assert (row.repository_id, row.done_until, row.generation) == (REPOSITORY, None, 0)


def test_a_requeued_backfill_starts_again_at_the_cycle_start(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    world.get_one(Backfill, REPOSITORY).done_until = datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC)
    world.flush()
    backfill.requeue(world, [REPOSITORY])
    assert _run(sessions, app) is True
    assert _first_range(github) == "2026-11-10T00:00:00Z"


def test_a_backfill_never_starts_before_the_account_s_data_was_deleted(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    world.get_one(Account, ORG).data_deleted_at = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    world.flush()
    backfill.requeue(world, [REPOSITORY])
    assert _run(sessions, app) is True
    assert _first_range(github) == "2026-11-12T10:00:00Z"


def test_a_requeue_during_a_running_pass_is_not_undone(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    clock: list[datetime],
) -> None:
    # A lost event re-queues the repository while the pass lists its first day.
    github.hooks["runs"] = lambda: _requeued(world)
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    # The old pass wrote no progress and did not finish: the re-queue stands.
    assert (row.done_until, row.generation) == (None, 1)
    assert _backfilled_at(world) is None
    later = NOW + timedelta(minutes=30)
    clock[0] = later
    github.requests.clear()
    assert _run(sessions, app, later) is True
    assert _first_range(github) == "2026-11-10T00:00:00Z"
    assert _row(world) is None
    assert _backfilled_at(world) == later


def _requeued(session: Session) -> None:
    backfill.requeue(session, [REPOSITORY])


def test_a_requeue_while_a_run_is_stored_stops_the_pass(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    github.runs[REPOSITORY] = [run_item(701, "2026-11-12T09:00:00Z")]
    github.jobs = {701: [job_item(9001, 701)]}
    github.hooks["jobs:701"] = lambda: _requeued(world)
    assert _run(sessions, app) is True
    assert _jobs(world) == set()
    assert (
        "backfill: repository 5001 was re-queued or taken over while it was backfilled; stopped"
        in caplog.text
    )


def test_a_pass_with_an_unchanged_generation_saves_progress_and_finishes(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-queued once before the pass: the pass claims that generation and works as before.
    backfill.requeue(world, [REPOSITORY])
    monkeypatch.setattr(backfill, "RUNS_PER_PASS", 1)
    github.runs[REPOSITORY] = [
        run_item(701, "2026-11-10T09:00:00Z"),
        run_item(702, "2026-11-11T09:00:00Z"),
    ]
    github.jobs = {701: [job_item(9001, 701)], 702: [job_item(9002, 702)]}
    assert _run(sessions, app) is True
    row = _row(world)
    assert row is not None
    assert (row.done_until, row.generation) == (datetime(2026, 11, 10, 23, 59, 59, tzinfo=UTC), 1)
    monkeypatch.setattr(backfill, "RUNS_PER_PASS", 200)
    assert _run(sessions, app) is True
    assert _row(world) is None
    assert _backfilled_at(world) == NOW
    assert _jobs(world) == {(9001, 1), (9002, 1)}


def test_a_lost_job_event_s_backfill_finds_a_job_of_a_run_created_days_before(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    # The repository was backfilled; then a job of a run created on the 10th completed on the
    # 14th, and its delivery and our redelivery both failed.
    world.execute(delete(Backfill))
    world.get_one(Repository, REPOSITORY).backfilled_at = NOW - timedelta(days=4)
    world.flush()
    guid = "00000000-0000-0000-0000-000000000077"
    deliveries = [
        {
            "id": 31,
            "guid": guid,
            "delivered_at": "2026-11-14T10:00:00Z",
            "redelivery": False,
            "status_code": 502,
            "event": "workflow_job",
            "installation_id": INSTALLATION,
            "repository_id": REPOSITORY,
        },
        {
            "id": 32,
            "guid": guid,
            "delivered_at": "2026-11-14T11:30:00Z",
            "redelivery": True,
            "status_code": 502,
            "event": "workflow_job",
            "installation_id": INSTALLATION,
            "repository_id": REPOSITORY,
        },
    ]
    github.override = lambda request: (
        httpx2.Response(200, json=deliveries)
        if request.url.path == "/app/hook/deliveries"
        else None
    )
    github.runs[REPOSITORY] = [run_item(701, "2026-11-10T08:00:00Z")]
    github.jobs = {701: [job_item(9001, 701)]}
    assert redeliver.redeliver(sessions, app, NOW, NOW - timedelta(hours=1)) is True
    assert _run(sessions, app) is True
    assert _jobs(world) == {(9001, 1)}


def test_a_backfilled_job_created_before_the_account_s_deletion_is_not_stored(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    # The run is listed (created after the deletion), but its job was created before it.
    world.get_one(Account, ORG).data_deleted_at = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    world.flush()
    github.runs[REPOSITORY] = [run_item(701, "2026-11-13T13:00:00Z")]
    github.jobs = {701: [job_item(9001, 701)]}
    assert _run(sessions, app) is True
    assert _jobs(world) == set()


def test_a_pass_whose_lease_was_taken_over_never_undoes_a_requeue(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    old = backfill.claim(sessions, NOW)
    assert old is not None
    old_target = backfill._target(sessions, old, NOW)
    assert old_target is not None
    # Its lease runs out; another pass takes the row over and finishes it.
    later = NOW + backfill.LEASE
    assert _run(sessions, app, later) is True
    assert _row(world) is None
    # A lost event re-queues it: the new row starts at generation 0 again.
    backfill.requeue(world, [REPOSITORY])
    # The old pass comes back and writes its progress and its finish: both change nothing.
    backfill._save_progress(sessions, old_target, datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC))
    backfill._finish(sessions, old_target, NOW)
    row = _row(world)
    assert row is not None
    assert (row.done_until, row.generation) == (None, 0)


def test_a_requeue_waits_for_delete_my_data_instead_of_deadlocking(engine: Engine) -> None:
    # Two connections need committed rows; they are deleted again at the end. One connection
    # does Delete my data's steps in its order: it locks both repositories, and after a re-queue
    # has started, deletes their queued rows. The re-queue, locking the repositories first, waits.
    with engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=7981, login="example-7981", type="User", github_plan="GitHub Free"
            )
        )
        connection.execute(insert(Installation).values(id=4981, account_id=7981, created_at=NOW))
        for repository_id in (5981, 5982):
            connection.execute(
                insert(Repository).values(
                    id=repository_id, installation_id=4981, name="app", private=True
                )
            )
        # One repository is queued, the other not.
        connection.execute(insert(Backfill).values(repository_id=5981))
    try:
        with engine.connect() as deleting, ThreadPoolExecutor(max_workers=1) as pool:
            held = deleting.begin()
            deleting.execute(text("SET LOCAL lock_timeout = '3s'"))
            deleting.execute(
                text("SELECT id FROM repository WHERE id IN (5981, 5982) ORDER BY id FOR UPDATE")
            )

            def requeue() -> int:
                with sessionmaker(bind=engine).begin() as session:
                    session.execute(text("SET LOCAL lock_timeout = '3s'"))
                    return backfill.requeue(session, [5981, 5982])

            requeuing = pool.submit(requeue)
            time.sleep(0.5)
            deleting.execute(text("DELETE FROM backfill WHERE repository_id IN (5981, 5982)"))
            held.commit()
            assert requeuing.result(timeout=10) == 2
    finally:
        with engine.begin() as connection:
            connection.execute(delete(Account).where(Account.id == 7981))


def test_a_run_of_a_pass_whose_lease_was_taken_over_stores_nothing(
    sessions: sessionmaker[Session], world: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    old = backfill.claim(sessions, NOW)
    assert old is not None
    old_target = backfill._target(sessions, old, NOW)
    assert old_target is not None
    # Its lease runs out and another pass takes the row over, with the same generation.
    assert backfill.claim(sessions, NOW + backfill.LEASE) is not None
    job = schemas.WorkflowJob.model_validate(job_item(9001, 701))
    assert backfill._store_run(sessions, old_target, [job]) is False
    assert _jobs(world) == set()
    assert "re-queued or taken over while it was backfilled; stopped" in caplog.text


def _wait_for_lock_waits(engine: Engine, count: int) -> None:
    """Wait, at most 10 seconds, until ``count`` sessions of the test database wait on a lock."""
    for _ in range(100):
        # A fresh connection each time: a transaction keeps its first view of the activity.
        with engine.connect() as watching:
            waiting = watching.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting is not None and waiting >= count:
            return
        time.sleep(0.1)
    raise AssertionError(f"fewer than {count} sessions waited on a lock")


def test_delete_my_data_locks_repositories_in_the_order_a_requeue_does(engine: Engine) -> None:
    # Two connections need committed rows; they are deleted again at the end. Twelve
    # repositories are stored in a scrambled order, so a query without ORDER BY returns them in
    # an order of the plan's choosing, on the pinned Postgres not ID order. A third connection
    # holds the highest; Delete my data starts and waits for it, then a re-queue starts and waits
    # too. Locking in ID order, the re-queue waits behind Delete my data; in the plan's order,
    # the pair deadlocks.
    scrambled = [5990, 5985, 5994, 5988, 5996, 5986, 5992, 5989, 5995, 5987, 5993, 5991]
    with engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=7985, login="example-7985", type="User", github_plan="GitHub Free"
            )
        )
        connection.execute(insert(Installation).values(id=4985, account_id=7985, created_at=NOW))
        for repository_id in scrambled:
            connection.execute(
                insert(Repository).values(
                    id=repository_id, installation_id=4985, name="app", private=True
                )
            )
    try:
        with engine.connect() as holding, ThreadPoolExecutor(max_workers=2) as pool:
            held = holding.begin()
            holding.execute(text("SELECT id FROM repository WHERE id = 5996 FOR UPDATE"))

            def delete_mine() -> None:
                with sessionmaker(bind=engine).begin() as session:
                    session.execute(text("SET LOCAL lock_timeout = '5s'"))
                    retention.delete_my_data(session, 7985)

            def requeue() -> int:
                with sessionmaker(bind=engine).begin() as session:
                    session.execute(text("SET LOCAL lock_timeout = '5s'"))
                    return backfill.requeue(session, sorted(scrambled))

            deleting = pool.submit(delete_mine)
            _wait_for_lock_waits(engine, 1)
            requeuing = pool.submit(requeue)
            _wait_for_lock_waits(engine, 2)
            held.commit()
            deleting.result(timeout=15)
            assert requeuing.result(timeout=15) == len(scrambled)
    finally:
        with engine.begin() as connection:
            connection.execute(delete(Account).where(Account.id == 7985))
