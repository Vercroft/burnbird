"""The hourly reconciliation against a fake api.github.com, through the real App client.

The fake lists invented installations and repositories, answers each token request with a token
it maps back to its installation, and can fail a page, refuse a token or run a hook while a list
is being fetched. No account is real; tokens are built from parts at run time.
"""

import logging
import secrets
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.github import reconcile
from burnbird.github.client import API_HOST, GithubAnswerError, GithubApp
from burnbird.models import Account, Installation, Job, Repository, WorkerTask

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
API = f"https://{API_HOST}"

type Body = dict[str, Any]
type Hook = Callable[[], None]


def installation_item(
    installation_id: int,
    *,
    kind: str = "Organization",
    suspended: bool = False,
    account_id: int | None = None,
) -> Body:
    """One entry of ``GET /app/installations``, with fields Burnbird ignores.

    The account's ID is the installation's minus 1000 unless given.
    """
    account_id = installation_id - 1000 if account_id is None else account_id
    return {
        "id": installation_id,
        "account": {"login": f"example-{account_id}", "id": account_id, "type": kind},
        "repository_selection": "selected",
        "app_id": 1,
        "target_type": kind,
        "permissions": {"actions": "read", "metadata": "read"},
        "events": ["workflow_job"],
        "created_at": "2026-11-13T09:00:00Z",
        "suspended_at": "2026-11-14T08:00:00Z" if suspended else None,
        "suspended_by": None,
    }


def repository_item(repository_id: int, *, private: bool = True) -> Body:
    """One entry of ``GET /installation/repositories``."""
    return {
        "id": repository_id,
        "name": f"example-app-{repository_id}",
        "full_name": f"example-org/example-app-{repository_id}",
        "private": private,
    }


class FakeGithub:
    """``GET /app/installations``, token requests and ``GET /installation/repositories``."""

    def __init__(self) -> None:
        self.installations: list[Body] = []
        self.repositories: dict[int, list[Body]] = {}
        # Each list's items are split into pages of this size, linked with rel="next".
        self.page_size = 100
        # The repository list's total_count, when it should differ from the items it holds.
        self.total_count: int | None = None
        self.failing: set[str] = set()
        self.refused: set[int] = set()
        self.hooks: dict[str, Hook] = {}
        # Installed, so ``GET /app/installations/{id}`` finds them, but missing from the list.
        self.unlisted: set[int] = set()
        self.tokens: dict[str, int] = {}
        self.requests: list[httpx2.Request] = []
        # Answers a request itself when it returns a response.
        self.override: Callable[[httpx2.Request], httpx2.Response | None] = lambda _: None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if (answer := self.override(request)) is not None:
            return answer
        path = request.url.path
        if request.method == "POST":
            installation_id = int(path.split("/")[3])
            if installation_id in self.refused:
                return httpx2.Response(403, json={"message": "This installation is suspended"})
            token = "gh" + "s_" + secrets.token_urlsafe(30)
            self.tokens[token] = installation_id
            expires = (NOW + timedelta(hours=1)).isoformat()
            return httpx2.Response(201, json={"token": token, "expires_at": expires})
        if path == "/app/installations":
            return self._page(request, "installations", self.installations, wrap=None)
        if path.startswith("/app/installations/"):
            installation_id = int(path.rsplit("/", 1)[1])
            listed = {item.get("id") for item in self.installations}
            if installation_id in listed | self.unlisted:
                return httpx2.Response(200, json=installation_item(installation_id))
            return httpx2.Response(404, json={"message": "Not Found"})
        installation_id = self.tokens[request.headers["Authorization"].removeprefix("Bearer ")]
        name = f"repositories:{installation_id}"
        return self._page(
            request, name, self.repositories.get(installation_id, []), wrap="repositories"
        )

    def _page(
        self, request: httpx2.Request, name: str, items: list[Body], wrap: str | None
    ) -> httpx2.Response:
        page = int(request.url.params.get("page", "1"))
        if f"{name}:{page}" in self.failing:
            return httpx2.Response(500)
        if page == 1 and name in self.hooks:
            self.hooks[name]()
        chunk = items[(page - 1) * self.page_size : page * self.page_size]
        headers = {}
        if page * self.page_size < len(items):
            next_url = f"{API}{request.url.path}?per_page=100&page={page + 1}"
            headers["Link"] = f'<{next_url}>; rel="next"'
        total = len(items) if self.total_count is None else self.total_count
        body: Any = chunk if wrap is None else {"total_count": total, wrap: chunk}
        return httpx2.Response(200, headers=headers, json=body)


@pytest.fixture
def github() -> FakeGithub:
    return FakeGithub()


@pytest.fixture
def app(rsa_key: RSAKey, github: FakeGithub) -> Iterator[GithubApp]:
    ticks = iter(range(10**6))
    app = GithubApp(
        "Iv23.example",
        rsa_key,
        transport=httpx2.MockTransport(github.handle),
        clock=lambda: NOW,
        monotonic=lambda: float(next(ticks)),
        sleep=lambda _: None,
    )
    yield app
    app.close()


@pytest.fixture
def store(session: Session) -> Callable[..., None]:
    """Store an installation of an invented organisation, its repositories and one job each."""

    def add(
        installation_id: int, repositories: tuple[int, ...] = (), account_id: int | None = None
    ) -> None:
        account_id = installation_id - 1000 if account_id is None else account_id
        if session.get(Account, account_id) is None:
            session.add(
                Account(
                    id=account_id,
                    login=f"example-{account_id}",
                    type="Organization",
                    github_plan="GitHub Free for organizations",
                )
            )
            session.flush()
        session.add(Installation(id=installation_id, account_id=account_id, created_at=NOW))
        session.flush()
        for repository_id in repositories:
            session.add(
                Repository(
                    id=repository_id, installation_id=installation_id, name="app", private=True
                )
            )
            session.flush()
            session.add(_job(repository_id))
        session.flush()

    return add


def _job(repository_id: int) -> Job:
    return Job(
        id=repository_id * 10,
        run_attempt=1,
        run_id=repository_id * 100,
        repository_id=repository_id,
        workflow_name="ci",
        labels=["ubuntu-latest"],
        runner_group_name="GitHub Actions",
        runner_name="GitHub Actions 1",
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=2),
        conclusion="success",
        provider="github",
        runner_class="actions_linux",
        billed_minutes=Decimal(2),
        amount=Decimal("0.012"),
    )


def _run(sessions: sessionmaker[Session], app: GithubApp) -> tuple[bool, datetime | None]:
    return reconcile.reconcile(sessions, app, NOW)


def _installations(session: Session) -> set[int]:
    session.expire_all()
    return set(session.scalars(select(Installation.id)))


def _repositories(session: Session, installation_id: int | None = None) -> set[int]:
    session.expire_all()
    query = select(Repository.id)
    if installation_id is not None:
        query = query.where(Repository.installation_id == installation_id)
    return set(session.scalars(query))


def _jobs(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Job)) or 0


def _synced(session: Session, installation_id: int) -> datetime | None:
    session.expire_all()
    return session.scalar(
        select(Installation.repositories_synced_at).where(Installation.id == installation_id)
    )


# Both ways.


def test_a_stored_installation_github_no_longer_lists_is_uninstalled_with_its_jobs(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    for installation_id in (4001, 4002, 4003):
        store(installation_id, (installation_id + 1000,))
    github.installations = [installation_item(4001), installation_item(4002)]
    github.repositories = {4001: [repository_item(5001)], 4002: [repository_item(5002)]}
    assert _run(sessions, app) == (True, None)
    assert _installations(session) == {4001, 4002}
    assert _repositories(session) == {5001, 5002}
    assert _jobs(session) == 2


def test_a_listed_installation_we_lack_is_created(
    sessions: sessionmaker[Session], session: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001, private=False)]}
    assert _run(sessions, app) == (True, None)
    account = session.get(Account, 3001)
    assert account is not None
    assert (account.login, account.github_plan) == (
        "example-3001",
        "GitHub Free for organizations",
    )
    repository = session.get(Repository, 5001)
    assert repository is not None
    assert (repository.installation_id, repository.private) == (4001, False)
    assert _synced(session, 4001) == NOW


def test_a_stored_repository_no_longer_listed_is_removed_with_its_jobs(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001, 5002))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001)]}
    assert _run(sessions, app) == (True, None)
    assert _repositories(session) == {5001}
    assert _jobs(session) == 1


def test_a_listed_repository_is_stored_and_a_stored_one_updated(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001,))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001, private=False), repository_item(5002)]}
    assert _run(sessions, app) == (True, None)
    assert _repositories(session, 4001) == {5001, 5002}
    updated = session.get(Repository, 5001)
    assert updated is not None
    assert (updated.name, updated.private) == ("example-app-5001", False)


def test_every_page_of_both_lists_is_read(
    sessions: sessionmaker[Session], session: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.page_size = 2
    github.installations = [installation_item(4000 + n) for n in range(1, 6)]
    github.repositories = {4001: [repository_item(5000 + n) for n in range(1, 6)]}
    assert _run(sessions, app) == (True, None)
    assert _installations(session) == {4001, 4002, 4003, 4004, 4005}
    assert _repositories(session, 4001) == {5001, 5002, 5003, 5004, 5005}


# What GitHub lists that Burnbird does not handle, and failures.


def test_an_enterprise_item_is_neither_stored_nor_a_reason_to_uninstall(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    # A stored installation listed in a shape Burnbird cannot parse stays.
    store(4001, (5001,))
    github.installations = [
        installation_item(4001, kind="Enterprise"),
        installation_item(4002, kind="Enterprise"),
        installation_item(4003),
    ]
    assert _run(sessions, app) == (True, None)
    assert _installations(session) == {4001, 4003}
    assert _repositories(session) == {5001}
    # Listed, so not even asked about.
    assert all(r.url.path != "/app/installations/4001" for r in github.requests)


def test_an_item_without_an_id_changes_nothing(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001,))
    store(4002, (5002,))
    github.installations = [installation_item(4003), {"account": {"id": 1}}]
    with pytest.raises(GithubAnswerError, match="without an ID"):
        _run(sessions, app)
    assert _installations(session) == {4001, 4002}


def test_a_failed_page_of_installations_changes_nothing(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001,))
    store(4002, (5002,))
    github.page_size = 1
    github.installations = [installation_item(4003), installation_item(4001)]
    github.failing = {"installations:2"}
    with pytest.raises(GithubAnswerError, match="answered 500"):
        _run(sessions, app)
    assert _installations(session) == {4001, 4002}


def test_a_failed_page_of_repositories_changes_nothing_of_that_installation(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store(4001, (5001, 5002))
    github.page_size = 1
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5003), repository_item(5001)]}
    github.failing = {"repositories:4001:2"}
    assert _run(sessions, app) == (False, None)
    assert _repositories(session) == {5001, 5002}
    assert _synced(session, 4001) is None
    assert "installation 4001 failed; skipped" in caplog.text


def test_an_installation_stored_during_the_fetch_is_kept(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001,))
    store(4002, (5002,))
    github.installations = [installation_item(4001), installation_item(4002)]
    github.repositories = {4001: [repository_item(5001)], 4002: [repository_item(5002)]}
    # A webhook stores 4003 after GitHub's list was made, while it is being fetched.
    github.hooks["installations"] = lambda: store(4003, (5003,))
    assert _run(sessions, app) == (True, None)
    assert _installations(session) == {4001, 4002, 4003}
    assert _repositories(session) == {5001, 5002, 5003}


def test_a_repository_stored_during_the_fetch_is_kept(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001,))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001)]}

    def job_arrives() -> None:
        session.add(Repository(id=5009, installation_id=4001, name="new", private=True))
        session.flush()

    github.hooks["repositories:4001"] = job_arrives
    assert _run(sessions, app) == (True, None)
    assert _repositories(session, 4001) == {5001, 5009}


def test_suspension_is_synced_and_a_suspended_installation_is_skipped(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001, 5002))
    store(4002, (5003,))
    session.get_one(Installation, 4002).suspended = True
    session.flush()
    github.installations = [installation_item(4001, suspended=True), installation_item(4002)]
    github.repositories = {4002: [repository_item(5003)]}
    assert _run(sessions, app) == (True, None)
    session.expire_all()
    assert session.get_one(Installation, 4001).suspended is True
    assert session.get_one(Installation, 4002).suspended is False
    # No token is asked for a suspended installation, and nothing of it changes.
    assert all("/4001/" not in str(request.url) for request in github.requests)
    assert _repositories(session, 4001) == {5001, 5002}


def test_a_refused_token_skips_the_installation_with_a_warning_and_deletes_nothing(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store(4001, (5001, 5002))
    store(4002, (5003, 5004))
    github.installations = [installation_item(4001), installation_item(4002)]
    github.repositories = {4002: [repository_item(5003)]}
    # GitHub still lists 4001 as not suspended, but refuses its token.
    github.refused = {4001}
    assert _run(sessions, app) == (False, None)
    assert _repositories(session, 4001) == {5001, 5002}
    assert _repositories(session, 4002) == {5003}
    assert _synced(session, 4001) is None
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == (
        "reconcile: installation 4001 refused a token (suspended?); skipped"
    )


def test_one_installations_failure_leaves_the_others_reconciled(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001, 5002))
    store(4002, (5003, 5004))
    github.installations = [installation_item(4001), installation_item(4002)]
    github.repositories = {4001: [repository_item(5001)], 4002: [repository_item(5003)]}
    github.failing = {"repositories:4001:1"}
    assert _run(sessions, app) == (False, None)
    assert _repositories(session, 4001) == {5001, 5002}
    assert _repositories(session, 4002) == {5003}
    assert _synced(session, 4002) == NOW


def test_a_rate_limited_installation_is_skipped_and_its_resume_time_kept(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001, 5002))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001)]}
    github.override = lambda request: (
        httpx2.Response(429, headers={"retry-after": "600"})
        if request.url.path == "/installation/repositories"
        else None
    )
    assert _run(sessions, app) == (False, NOW + timedelta(minutes=10))
    assert _repositories(session, 4001) == {5001, 5002}


# The mass-deletion guard.


def test_an_empty_installation_list_uninstalls_nothing(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Two stored would be within the limit of two: only the empty-list rule holds them.
    store(4001, (5001,))
    store(4002, (5002,))
    github.installations = []
    assert _run(sessions, app) == (False, None)
    assert _installations(session) == {4001, 4002}
    assert _jobs(session) == 2
    [error] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert (
        error.getMessage() == "reconcile: GitHub lists no installation, 2 stored; none uninstalled"
    )


@pytest.mark.parametrize(
    ("stored", "gone", "uninstalled"), [(30, 3, True), (30, 4, False), (5, 2, True), (5, 3, False)]
)
def test_more_uninstalls_than_the_limit_uninstall_none(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    stored: int,
    gone: int,
    uninstalled: bool,
) -> None:
    # The limit is max(2, 10% of stored): 3 of 30, and 2 of 5.
    ids = [4001 + n for n in range(stored)]
    for installation_id in ids:
        store(installation_id)
    github.installations = [installation_item(i) for i in ids[gone:]]
    github.installations.append(installation_item(4900))
    complete, _ = _run(sessions, app)
    expected = set(ids[gone:]) | {4900} if uninstalled else set(ids) | {4900}
    assert _installations(session) == expected
    assert complete is uninstalled
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    if uninstalled:
        assert errors == []
    else:
        [error] = errors
        # Counts only: no installation ID or login.
        assert error.args == (gone, stored, max(2, stored // 10))
        assert all(str(i) not in error.getMessage() for i in ids)


def test_an_empty_repository_list_removes_nothing_while_repositories_are_stored(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store(4001, (5001, 5002))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: []}
    assert _run(sessions, app) == (False, None)
    assert _repositories(session, 4001) == {5001, 5002}
    assert _jobs(session) == 2
    assert _synced(session, 4001) is None
    [error] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error.getMessage() == (
        "reconcile: installation 4001 lists no repository, 2 stored; none removed"
    )


def test_an_empty_repository_list_with_nothing_stored_is_synced(
    sessions: sessionmaker[Session], session: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.installations = [installation_item(4001)]
    assert _run(sessions, app) == (True, None)
    assert _synced(session, 4001) == NOW


def test_creates_and_updates_still_apply_when_the_guard_holds(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    for installation_id in (4001, 4002, 4003):
        store(installation_id, (installation_id + 1000,))
    # All three gone is over the limit of two; a new installation is still created.
    github.installations = [installation_item(4004, suspended=True)]
    assert _run(sessions, app) == (False, None)
    assert _installations(session) == {4001, 4002, 4003, 4004}
    assert session.get_one(Installation, 4004).suspended is True


def test_the_next_run_tries_again(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    for installation_id in (4001, 4002, 4003):
        store(installation_id)
    github.installations = []
    assert reconcile.run(sessions, app, NOW) is True
    task = session.get_one(WorkerTask, reconcile.TASK)
    assert (task.next_run_at, task.last_success_at) == (NOW + reconcile.INTERVAL, None)
    assert _installations(session) == {4001, 4002, 4003}
    # A person checks; GitHub's list is right again by the next run, an hour later.
    github.installations = [installation_item(4001), installation_item(4002)]
    later = NOW + reconcile.INTERVAL
    assert reconcile.run(sessions, app, later) is True
    assert _installations(session) == {4001, 4002}
    session.expire_all()
    assert session.get_one(WorkerTask, reconcile.TASK).last_success_at == later


# The task.


def test_a_run_is_not_due_again_within_the_hour(
    sessions: sessionmaker[Session], app: GithubApp, github: FakeGithub
) -> None:
    assert reconcile.run(sessions, app, NOW) is True
    calls = len(github.requests)
    assert reconcile.run(sessions, app, NOW + timedelta(minutes=59)) is False
    assert len(github.requests) == calls


def test_a_rate_limited_installation_list_puts_the_run_off(
    sessions: sessionmaker[Session], session: Session, app: GithubApp, github: FakeGithub
) -> None:
    github.override = lambda _: httpx2.Response(403, headers={"retry-after": "7200"})
    assert reconcile.run(sessions, app, NOW) is True
    task = session.get_one(WorkerTask, reconcile.TASK)
    assert (task.next_run_at, task.last_success_at) == (NOW + timedelta(hours=2), None)


# The account rule, the uninstall check and the repository count (from the independent review).


def test_a_reinstall_during_the_fetch_keeps_the_new_installation(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001,))
    github.installations = [installation_item(4001)]

    def reinstall() -> None:
        # A sign-in stores the account's new installation 4005 and drops 4001 while the old list
        # that still names 4001 is on its way.
        session.execute(delete(Job).where(Job.repository_id == 5001))
        session.execute(delete(Repository).where(Repository.installation_id == 4001))
        session.execute(delete(Installation).where(Installation.id == 4001))
        store(4005, (5005,), account_id=3001)

    github.hooks["installations"] = reinstall
    complete, _ = _run(sessions, app)
    assert complete is False
    assert _installations(session) == {4005}
    assert _repositories(session) == {5005}
    assert _jobs(session) == 1
    # Not stored, so its repositories are not asked for.
    assert all(r.method != "POST" for r in github.requests)


def test_an_installation_deleted_during_the_fetch_is_not_made_again(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001,))
    store(4002, (5002,))
    github.installations = [installation_item(4001), installation_item(4002)]
    github.repositories = {4001: [repository_item(5001)]}

    def uninstalled() -> None:
        retention.uninstall(session, 4002)

    github.hooks["installations"] = uninstalled
    complete, _ = _run(sessions, app)
    assert complete is False
    assert _installations(session) == {4001}


def test_a_listed_installation_of_an_account_with_another_stored_is_not_stored(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 4001 to 4003 are not listed (three is over the limit of two, so none is uninstalled), and
    # 4004 is the reinstall of 4001's account: storing it would uninstall 4001 past the guard.
    for installation_id in (4001, 4002, 4003):
        store(installation_id, (installation_id + 1000,))
    github.installations = [installation_item(4004, account_id=3001)]
    assert _run(sessions, app) == (False, None)
    assert _installations(session) == {4001, 4002, 4003}
    assert _jobs(session) == 3
    assert "none uninstalled" in caplog.text


def test_an_installation_missing_from_the_list_but_still_installed_is_kept(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    # Pages moved between two fetches, so 4002 is missing although GitHub still has it.
    store(4001, (5001,))
    store(4002, (5002,))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001)]}
    github.unlisted = {4002}
    complete, _ = _run(sessions, app)
    assert complete is False
    assert _installations(session) == {4001, 4002}
    assert _jobs(session) == 2


def test_only_an_installation_github_cannot_find_is_uninstalled(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001,))
    store(4002, (5002,))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001)]}
    assert _run(sessions, app) == (True, None)
    assert _installations(session) == {4001}
    assert [
        r.url.path
        for r in github.requests
        if r.url.path.startswith("/app/installations/") and r.method == "GET"
    ] == ["/app/installations/4002"]


def test_a_failed_check_of_a_missing_installation_keeps_it(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001,))
    store(4002, (5002,))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001)]}
    github.override = lambda request: (
        httpx2.Response(502) if request.url.path == "/app/installations/4002" else None
    )
    complete, _ = _run(sessions, app)
    assert complete is False
    assert _installations(session) == {4001, 4002}


def test_a_repository_list_shorter_than_its_total_count_removes_nothing(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An item moved onto a page already read, so the list holds one fewer than GitHub counts.
    store(4001, (5001, 5002))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001)]}
    github.total_count = 2
    complete, _ = _run(sessions, app)
    assert complete is False
    assert _repositories(session, 4001) == {5001, 5002}
    assert _synced(session, 4001) is None
    assert "installation 4001 failed; skipped" in caplog.text


def test_updates_still_apply_when_the_guard_holds(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    for installation_id in (4001, 4002, 4003, 4004):
        store(installation_id, (installation_id + 1000,))
    github.installations = [installation_item(4004, suspended=True)]
    assert _run(sessions, app) == (False, None)
    assert _installations(session) == {4001, 4002, 4003, 4004}
    session.expire_all()
    assert session.get_one(Installation, 4004).suspended is True


# Repositories changed while their list is fetched.


def test_a_repository_removed_during_the_fetch_is_not_made_again(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store(4001, (5001, 5002))
    github.installations = [installation_item(4001)]
    github.repositories = {4001: [repository_item(5001), repository_item(5002)]}

    # A webhook removes 5002 after GitHub's list was made, while it is being fetched.
    def removed() -> None:
        retention.remove_repositories(session, 4001, [5002])

    github.hooks["repositories:4001"] = removed
    complete, _ = _run(sessions, app)
    assert complete is False
    assert _repositories(session, 4001) == {5001}
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == (
        "reconcile: installation 4001: repositories [5002] changed while listed; not stored"
    )


def test_a_repository_moved_during_the_fetch_stays_with_its_new_installation(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
) -> None:
    store(4001, (5001, 5002))
    store(4002, (5003,))
    # 4002 is reconciled first; then, while 4001's list is fetched, 5002 moves to 4002.
    github.installations = [installation_item(4002), installation_item(4001)]
    github.repositories = {
        4002: [repository_item(5003)],
        4001: [repository_item(5001), repository_item(5002)],
    }

    def moved() -> None:
        session.get_one(Repository, 5002).installation_id = 4002
        session.flush()

    github.hooks["repositories:4001"] = moved
    complete, _ = _run(sessions, app)
    assert complete is False
    assert _repositories(session, 4001) == {5001}
    assert _repositories(session, 4002) == {5002, 5003}


def test_an_installation_uninstalled_during_its_repository_fetch_writes_nothing(
    sessions: sessionmaker[Session],
    session: Session,
    app: GithubApp,
    github: FakeGithub,
    store: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store(4001, (5001,))
    store(4002, (5002,))
    github.installations = [installation_item(4001), installation_item(4002)]
    github.repositories = {
        4001: [repository_item(5001), repository_item(5009)],
        4002: [repository_item(5002), repository_item(5008)],
    }

    def uninstalled() -> None:
        retention.uninstall(session, 4001)

    github.hooks["repositories:4001"] = uninstalled
    complete, _ = _run(sessions, app)
    assert complete is False
    assert _installations(session) == {4002}
    assert _repositories(session) == {5002, 5008}
    assert _synced(session, 4002) == NOW
    assert all(record.exc_info is None for record in caplog.records)
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage() == (
        "reconcile: installation 4001 was uninstalled while its repositories were listed; "
        "nothing written"
    )
