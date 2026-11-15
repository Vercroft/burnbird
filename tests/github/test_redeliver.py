"""The hourly redelivery of failed webhook deliveries, against a fake api.github.com.

The fake lists invented deliveries newest first, page by page, as GitHub's own example script reads
them, answers a redelivery request with 202 unless a test plants another answer, and records what
was asked. No account is real.
"""

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import Engine, delete, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from burnbird.github import redeliver
from burnbird.github.client import API_HOST, GithubAnswerError, GithubApp, RateLimitedError
from burnbird.models import (
    Account,
    Backfill,
    Installation,
    RedeliveryRequest,
    Repository,
    WebhookDelivery,
    WorkerTask,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
API = f"https://{API_HOST}"
INSTALLATION, OTHER_INSTALLATION = 4001, 4002
REPOSITORY = 5001

type Body = dict[str, Any]


def attempt(
    delivery_id: int,
    guid: uuid.UUID,
    at: datetime,
    *,
    status_code: int | None = 500,
    redelivery: bool = False,
    event: str = "workflow_job",
    installation_id: int | None = INSTALLATION,
    repository_id: int | None = REPOSITORY,
) -> Body:
    """One entry of ``GET /app/hook/deliveries``, with fields Burnbird ignores."""
    item: Body = {
        "id": delivery_id,
        "guid": str(guid),
        "delivered_at": at.isoformat(),
        "redelivery": redelivery,
        "duration": 0.31,
        "status": "OK" if status_code is not None and 200 <= status_code < 400 else "failed",
        "event": event,
        "action": "completed",
        "installation_id": installation_id,
        "repository_id": repository_id,
        "throttled_at": None,
    }
    if status_code is not None:
        item["status_code"] = status_code
    return item


class FakeGithub:
    """The App's deliveries list, newest first, and its redelivery endpoint."""

    def __init__(self) -> None:
        self.deliveries: list[Body] = []
        self.page_size = 100
        self.answers: dict[int, httpx2.Response] = {}
        self.asked: list[int] = []
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/attempts"):
            delivery_id = int(path.split("/")[4])
            self.asked.append(delivery_id)
            return self.answers.get(delivery_id, httpx2.Response(202, json={}))
        if path == "/app/hook/deliveries":
            ordered = sorted(self.deliveries, key=lambda d: d["delivered_at"], reverse=True)
            start = int(request.url.params.get("after", "0"))
            page = ordered[start : start + self.page_size]
            headers = {}
            if start + self.page_size < len(ordered):
                following = (
                    f"{API}/app/hook/deliveries?per_page=100&after={start + self.page_size}"
                )
                headers["Link"] = f'<{following}>; rel="next"'
            return httpx2.Response(200, headers=headers, json=page)
        if path == "/app/installations":
            return httpx2.Response(200, json=[])
        return httpx2.Response(404)

    def list_requests(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path == "/app/hook/deliveries"]


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
    """An installation with one repository, already backfilled, and a suspended installation."""
    for account_id, installation_id, repository_id, suspended in (
        (3001, INSTALLATION, REPOSITORY, False),
        (3002, OTHER_INSTALLATION, 5002, True),
    ):
        session.add(
            Account(
                id=account_id,
                login=f"example-{account_id}",
                type="Organization",
                github_plan="GitHub Free for organizations",
            )
        )
        session.flush()
        session.add(
            Installation(
                id=installation_id, account_id=account_id, created_at=NOW, suspended=suspended
            )
        )
        session.flush()
        session.add(
            Repository(
                id=repository_id,
                installation_id=installation_id,
                name="app",
                private=True,
                backfilled_at=NOW - timedelta(days=5),
            )
        )
    session.flush()
    return session


def _run(
    sessions: sessionmaker[Session],
    app: GithubApp,
    last_success_at: datetime | None = NOW - timedelta(hours=1),
    now: datetime = NOW,
) -> bool:
    return redeliver.redeliver(sessions, app, now, last_success_at)


def _guid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _queued(session: Session) -> dict[int, tuple[datetime | None, int]]:
    session.expire_all()
    return {
        row.repository_id: (row.done_until, row.generation)
        for row in session.scalars(select(Backfill))
    }


# Asking.


def test_a_failed_delivery_is_asked_for_once_by_its_newest_attempt(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50)),
        attempt(12, _guid(1), NOW - timedelta(minutes=40), status_code=502),
    ]
    assert _run(sessions, app) is True
    assert github.asked == [12]
    # The next run sees the same failures and asks nothing: the GUID is recorded.
    assert _run(sessions, app) is True
    assert github.asked == [12]
    world.expire_all()
    assert world.get(RedeliveryRequest, _guid(1)) is not None


@pytest.mark.parametrize("status_code", [400, 413])
def test_a_delivery_we_refused_with_400_or_413_is_never_asked(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    status_code: int,
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50), status_code=status_code),
        attempt(12, _guid(1), NOW - timedelta(minutes=40), status_code=500),
    ]
    assert _run(sessions, app) is True
    assert github.asked == []


@pytest.mark.parametrize(
    ("other", "reason"),
    [
        pytest.param({"status_code": 200}, "success", id="a success"),
        pytest.param({"status_code": 399}, "success", id="a 3xx"),
        pytest.param({"redelivery": True}, "asked", id="a redelivery attempt"),
    ],
)
def test_a_delivery_with_a_success_or_a_redelivery_is_not_asked(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    other: Body,
    reason: str,
) -> None:
    second = attempt(12, _guid(1), NOW - timedelta(minutes=40))
    second.update(other)
    github.deliveries = [attempt(11, _guid(1), NOW - timedelta(minutes=50)), second]
    assert _run(sessions, app) is True
    assert github.asked == [], reason


def test_a_delivery_that_reached_us_is_not_asked(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    # GitHub marked it failed (it timed out on its side), but we stored it.
    world.add(
        WebhookDelivery(
            id=_guid(1),
            received_at=NOW - timedelta(minutes=50),
            event="workflow_job",
            action="completed",
            payload={},
        )
    )
    world.flush()
    github.deliveries = [attempt(11, _guid(1), NOW - timedelta(minutes=50))]
    assert _run(sessions, app) is True
    assert github.asked == []


@pytest.mark.parametrize("installation_id", [4999, None])
def test_a_delivery_of_an_installation_not_stored_is_not_asked(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    installation_id: int | None,
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50), installation_id=installation_id)
    ]
    assert _run(sessions, app) is True
    assert github.asked == []


@pytest.mark.parametrize("status_code", [0, None])
def test_a_timed_out_attempt_counts_as_a_failure(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    status_code: int | None,
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50), status_code=status_code)
    ]
    assert _run(sessions, app) is True
    assert github.asked == [11]


def test_at_most_fifty_are_asked_and_the_window_stays(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [
        attempt(100 + n, _guid(100 + n), NOW - timedelta(minutes=50, seconds=n)) for n in range(52)
    ]
    assert _run(sessions, app) is False
    assert len(github.asked) == 50
    assert _run(sessions, app) is True
    assert len(github.asked) == 52


def test_a_redelivery_answered_422_is_logged_and_not_asked_again(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github.deliveries = [attempt(11, _guid(1), NOW - timedelta(minutes=50))]
    github.answers[11] = httpx2.Response(422, json={"message": "Validation failed"})
    assert _run(sessions, app) is True
    assert _run(sessions, app) is True
    assert github.asked == [11]
    assert "redeliver: delivery 11 answered 422; not asked again" in caplog.text


def test_a_rate_limited_redelivery_is_asked_again_on_a_later_run(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    clock: list[datetime],
) -> None:
    github.deliveries = [attempt(11, _guid(1), NOW - timedelta(minutes=50))]
    github.answers[11] = httpx2.Response(429, headers={"retry-after": "60"})
    with pytest.raises(RateLimitedError):
        _run(sessions, app)
    # GitHub refused it, so no redelivery happened: the record is removed again.
    world.expire_all()
    assert world.get(RedeliveryRequest, _guid(1)) is None
    del github.answers[11]
    later = NOW + timedelta(minutes=2)
    clock[0] = later
    assert _run(sessions, app, now=later) is True
    assert github.asked == [11, 11]
    world.expire_all()
    assert world.get(RedeliveryRequest, _guid(1)) is not None


# The window and the listing.


def test_the_window_is_three_days_or_since_an_hour_before_the_last_success(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(hours=2, minutes=1)),
        attempt(12, _guid(2), NOW - timedelta(hours=1, minutes=59)),
    ]
    # Last success 3 hours ago: the window starts an hour before it, at 4 hours ago.
    assert _run(sessions, app, last_success_at=NOW - timedelta(hours=3)) is True
    assert sorted(github.asked) == [11, 12]
    github.asked.clear()
    world.execute(delete(RedeliveryRequest))
    # Last success 1 hour ago: the window starts 2 hours ago; 11 is older.
    assert _run(sessions, app, last_success_at=NOW - timedelta(hours=1)) is True
    assert github.asked == [12]


def test_a_first_run_looks_back_three_days(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(days=3, minutes=1)),
        attempt(12, _guid(2), NOW - timedelta(days=2, hours=23)),
    ]
    assert _run(sessions, app, last_success_at=None) is True
    assert github.asked == [12]


def test_paging_stops_at_the_first_page_wholly_older_than_the_window(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.page_size = 2
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=10)),
        attempt(12, _guid(2), NOW - timedelta(minutes=20)),
        # This page has one item inside the window: the next page is read too.
        attempt(13, _guid(3), NOW - timedelta(minutes=30)),
        attempt(14, _guid(4), NOW - timedelta(hours=5)),
        attempt(15, _guid(5), NOW - timedelta(hours=6)),
        attempt(16, _guid(6), NOW - timedelta(hours=7)),
        attempt(17, _guid(7), NOW - timedelta(hours=8)),
    ]
    assert _run(sessions, app) is True
    assert len(github.list_requests()) == 3
    assert sorted(github.asked) == [11, 12, 13]


def test_a_deliveries_answer_not_as_documented_fails_without_its_values(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    planted = "planted-" + uuid.uuid4().hex
    bad = attempt(11, _guid(1), NOW - timedelta(minutes=50))
    bad["guid"] = planted
    github.deliveries = [bad]
    with pytest.raises(GithubAnswerError) as raised:
        _run(sessions, app)
    assert planted not in f"{raised.value} {raised.value!r}"
    assert raised.value.__suppress_context__


# Lost events and the gap rule.


def test_a_lost_job_event_requeues_its_repository_from_the_cycle_start(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    world.add(Backfill(repository_id=REPOSITORY, done_until=NOW - timedelta(days=1)))
    world.flush()
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50)),
        # The redelivery we asked for last run failed too: the event is lost.
        attempt(12, _guid(1), NOW - timedelta(minutes=20), redelivery=True),
    ]
    assert _run(sessions, app) is True
    assert _queued(world) == {REPOSITORY: (None, 1)}
    assert github.asked == []


def test_a_redelivery_that_failed_before_the_last_success_is_not_requeued_again(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(hours=2, minutes=30)),
        attempt(12, _guid(1), NOW - timedelta(hours=2, minutes=10), redelivery=True),
    ]
    assert _run(sessions, app, last_success_at=NOW - timedelta(hours=2)) is True
    assert _queued(world) == {}
    # It was asked for once already (its redelivery attempt shows it): never again.
    assert github.asked == []


def test_a_lost_event_that_is_not_a_job_requeues_nothing(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50), event="installation"),
        attempt(12, _guid(1), NOW - timedelta(minutes=20), event="installation", redelivery=True),
    ]
    assert _run(sessions, app) is True
    assert _queued(world) == {}


def test_a_gap_whose_overlap_reaches_past_three_days_requeues_every_repository(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    # Last success 71 hours ago: its hour of overlap would start before GitHub's 3 days.
    assert _run(sessions, app, last_success_at=NOW - timedelta(hours=71, seconds=1)) is True
    # Every repository of an installation that is not suspended.
    assert _queued(world) == {REPOSITORY: (None, 0)}


def test_a_gap_whose_overlap_fits_in_three_days_requeues_nothing(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    assert _run(sessions, app, last_success_at=NOW - timedelta(hours=71)) is True
    assert _queued(world) == {}


def test_old_redelivery_records_are_purged(
    sessions: sessionmaker[Session], world: Session, app: GithubApp
) -> None:
    world.add_all(
        [
            RedeliveryRequest(guid=_guid(1), requested_at=NOW - timedelta(days=3, seconds=1)),
            RedeliveryRequest(guid=_guid(2), requested_at=NOW - timedelta(days=2)),
        ]
    )
    world.flush()
    assert _run(sessions, app) is True
    world.expire_all()
    assert set(world.scalars(select(RedeliveryRequest.guid))) == {_guid(2)}


# The task.


def test_the_task_runs_hourly_and_keeps_the_window_when_incomplete(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [
        attempt(100 + n, _guid(100 + n), NOW - timedelta(minutes=50, seconds=n)) for n in range(51)
    ]
    assert redeliver.run(sessions, app, NOW) is True
    task = world.get_one(WorkerTask, redeliver.TASK)
    assert (task.next_run_at, task.last_success_at) == (NOW + redeliver.INTERVAL, None)
    assert redeliver.run(sessions, app, NOW + timedelta(minutes=59)) is False
    later = NOW + redeliver.INTERVAL
    assert redeliver.run(sessions, app, later) is True
    world.expire_all()
    assert world.get_one(WorkerTask, redeliver.TASK).last_success_at == later
    assert len(github.asked) == 51


def test_two_workers_at_once_ask_once(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [attempt(11, _guid(1), NOW - timedelta(minutes=50))]
    assert redeliver.run(sessions, app, NOW) is True
    assert redeliver.run(sessions, app, NOW) is False
    assert github.asked == [11]


def test_the_requests_recorded_count_the_asked_guids(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50)),
        attempt(21, _guid(2), NOW - timedelta(minutes=45)),
    ]
    assert _run(sessions, app) is True
    world.expire_all()
    assert world.scalar(select(func.count()).select_from(RedeliveryRequest)) == 2


# From the independent review.


def test_a_gap_is_requeued_once_however_many_runs_stay_incomplete(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    world.add(Backfill(repository_id=REPOSITORY))
    world.flush()
    github.deliveries = [
        attempt(100 + n, _guid(100 + n), NOW - timedelta(minutes=50, seconds=n))
        for n in range(120)
    ]
    gap = NOW - timedelta(days=4)
    # Each run hits the cap, so the last success, and the gap, stay the same.
    assert _run(sessions, app, last_success_at=gap) is False
    assert _run(sessions, app, last_success_at=gap) is False
    assert _queued(world) == {REPOSITORY: (None, 1)}


def test_a_lost_event_is_requeued_once_however_many_runs_see_it(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    world.add(Backfill(repository_id=REPOSITORY))
    world.flush()
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50)),
        attempt(12, _guid(1), NOW - timedelta(minutes=20), redelivery=True),
    ]
    since = NOW - timedelta(hours=1)
    assert _run(sessions, app, last_success_at=since) is True
    assert _run(sessions, app, last_success_at=since) is True
    assert _queued(world) == {REPOSITORY: (None, 1)}


def test_a_lost_event_on_a_first_run_is_requeued(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(hours=5)),
        attempt(12, _guid(1), NOW - timedelta(hours=4), redelivery=True),
    ]
    assert _run(sessions, app, last_success_at=None) is True
    assert _queued(world) == {REPOSITORY: (None, 0)}


def test_two_workers_on_two_connections_ask_once(engine: Engine, rsa_key: RSAKey) -> None:
    # Two connections need committed rows; they are deleted again at the end.
    with engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=3951,
                login="example-3951",
                type="Organization",
                github_plan="GitHub Free for organizations",
            )
        )
        connection.execute(insert(Installation).values(id=4951, account_id=3951, created_at=NOW))
    github = FakeGithub()
    github.deliveries = [
        attempt(
            11, _guid(951), NOW - timedelta(minutes=50), installation_id=4951, repository_id=None
        )
    ]
    apps = [
        GithubApp(
            "Iv23.example",
            rsa_key,
            transport=httpx2.MockTransport(github.handle),
            clock=lambda: NOW,
            sleep=lambda _: None,
        )
        for _ in range(2)
    ]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = [
                pool.submit(redeliver.run, sessionmaker(bind=engine), app, NOW) for app in apps
            ]
            claimed = [run.result(timeout=20) for run in runs]
        assert sorted(claimed) == [False, True]
        assert github.asked == [11]
    finally:
        for app in apps:
            app.close()
        with engine.begin() as connection:
            connection.execute(delete(Account).where(Account.id == 3951))
            connection.execute(delete(WorkerTask).where(WorkerTask.name == redeliver.TASK))
            connection.execute(
                delete(RedeliveryRequest).where(RedeliveryRequest.guid == _guid(951))
            )


# Job events that will not be resent.


@pytest.mark.parametrize("status_code", [400, 413])
def test_a_job_event_we_refused_requeues_its_repository_once(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    status_code: int,
) -> None:
    world.add(Backfill(repository_id=REPOSITORY))
    world.flush()
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50), status_code=status_code)
    ]
    assert _run(sessions, app) is True
    assert _run(sessions, app) is True
    assert github.asked == []
    assert _queued(world) == {REPOSITORY: (None, 1)}
    world.expire_all()
    record = world.get_one(RedeliveryRequest, _guid(1))
    assert (record.requested_at, record.requeued_at) == (NOW, NOW)


def _network_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("refused", request=request)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(httpx2.Response(422, json={"message": "Validation failed"}), id="422"),
        pytest.param(httpx2.Response(503), id="503"),
        pytest.param(None, id="a network error"),
    ],
)
def test_a_failed_request_for_a_job_event_requeues_its_repository_once(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    monkeypatch: pytest.MonkeyPatch,
    answer: httpx2.Response | None,
) -> None:
    world.add(Backfill(repository_id=REPOSITORY))
    world.flush()
    github.deliveries = [attempt(11, _guid(1), NOW - timedelta(minutes=50))]
    if answer is None:
        real = github.handle

        def failing(request: httpx2.Request) -> httpx2.Response:
            if request.method == "POST":
                github.asked.append(11)
                return _network_error(request)
            return real(request)

        monkeypatch.setattr(github, "handle", failing)
    else:
        github.answers[11] = answer
    assert _run(sessions, app) is True
    assert _run(sessions, app) is True
    assert github.asked == [11]
    assert _queued(world) == {REPOSITORY: (None, 1)}


def test_a_rate_limited_request_for_a_job_event_requeues_nothing(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    world.add(Backfill(repository_id=REPOSITORY))
    world.flush()
    github.deliveries = [attempt(11, _guid(1), NOW - timedelta(minutes=50))]
    github.answers[11] = httpx2.Response(429, headers={"retry-after": "60"})
    with pytest.raises(RateLimitedError):
        _run(sessions, app)
    assert _queued(world) == {REPOSITORY: (None, 0)}


@pytest.mark.parametrize(
    ("planted", "answer"),
    [
        pytest.param({"status_code": 400}, None, id="refused with 400"),
        pytest.param({"status_code": 413}, None, id="refused with 413"),
        pytest.param({}, httpx2.Response(422, json={}), id="a request answered 422"),
        pytest.param({}, httpx2.Response(503), id="a request answered 503"),
    ],
)
def test_an_installation_event_that_will_not_be_resent_requeues_nothing(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    planted: dict[str, Any],
    answer: httpx2.Response | None,
) -> None:
    github.deliveries = [
        attempt(11, _guid(1), NOW - timedelta(minutes=50), event="installation", **planted)
    ]
    if answer is not None:
        github.answers[11] = answer
    assert _run(sessions, app) is True
    assert _queued(world) == {}


def test_an_installation_event_whose_request_had_a_network_error_requeues_nothing(
    sessions: sessionmaker[Session],
    world: Session,
    app: GithubApp,
    github: FakeGithub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github.deliveries = [attempt(11, _guid(1), NOW - timedelta(minutes=50), event="installation")]
    real = github.handle

    def failing(request: httpx2.Request) -> httpx2.Response:
        return _network_error(request) if request.method == "POST" else real(request)

    monkeypatch.setattr(github, "handle", failing)
    assert _run(sessions, app) is True
    assert _queued(world) == {}


def test_a_job_event_already_requeued_is_not_requeued_by_a_later_failed_attempt(
    sessions: sessionmaker[Session], world: Session, app: GithubApp, github: FakeGithub
) -> None:
    world.add(Backfill(repository_id=REPOSITORY))
    world.flush()
    github.deliveries = [attempt(11, _guid(1), NOW - timedelta(minutes=50))]
    github.answers[11] = httpx2.Response(422, json={})
    assert _run(sessions, app) is True
    assert _queued(world) == {REPOSITORY: (None, 1)}
    # The next hour GitHub lists a redelivery attempt that failed, and one we refused.
    later = NOW + timedelta(hours=1)
    github.deliveries += [
        attempt(12, _guid(1), NOW - timedelta(minutes=5), redelivery=True, status_code=413)
    ]
    assert _run(sessions, app, last_success_at=NOW, now=later) is True
    assert github.asked == [11]
    assert _queued(world) == {REPOSITORY: (None, 1)}
