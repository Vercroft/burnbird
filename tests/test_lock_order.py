"""Two paths that lock the same rows never deadlock: each takes them in the lock order.

The order is written in ``retention.lock_repositories``. Each path runs on its own connection, so
the tests need committed rows; each test deletes them again at the end. One path is paused just
before one statement, holding the locks it took so far; the other starts meanwhile; then the
paused one goes on. Both must finish. A deadlock would make Postgres abort one of them, and the
test fails with its error.
"""

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import Connection, Engine, delete, event, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.auth.service import ListedRepository, SignIn, session_digest, store_sign_in
from burnbird.email.preferences import set_choice
from burnbird.github import backfill, reconcile, schemas
from burnbird.github.client import GithubApp
from burnbird.github.events import HANDLERS, store_job
from burnbird.models import (
    Account,
    Backfill,
    EmailPreference,
    GithubUser,
    Installation,
    Job,
    Repository,
    UserEmail,
    UserInstallation,
    UserRepository,
    UserSession,
)
from tests.github import fakes
from tests.github.fakes import FakeGithub, installation_item, repository_item

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _installation(installation_id: int, account_id: int, kind: str) -> schemas.Installation:
    return schemas.Installation.model_validate(
        {
            "id": installation_id,
            "account": {"id": account_id, "login": f"example-{account_id}", "type": kind},
            "created_at": NOW,
        }
    )


def _store(engine: Engine, installation: schemas.Installation, user_id: int) -> int:
    """Store the account, its installation, one repository and the user's lists; return its ID."""
    repository_id = installation.id + 1000
    with engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=installation.account.id,
                login=installation.account.login,
                type=installation.account.type,
                github_plan=schemas.DEFAULT_PLAN[installation.account.type],
            )
        )
        connection.execute(
            insert(Installation).values(
                id=installation.id, account_id=installation.account.id, created_at=NOW
            )
        )
        connection.execute(
            insert(Repository).values(
                id=repository_id, installation_id=installation.id, name="app", private=True
            )
        )
        connection.execute(
            insert(GithubUser).values(id=user_id, login=f"example-{user_id}", last_sign_in_at=NOW)
        )
        connection.execute(
            insert(UserInstallation).values(user_id=user_id, installation_id=installation.id)
        )
        connection.execute(
            insert(UserRepository).values(user_id=user_id, repository_id=repository_id, admin=True)
        )
    return repository_id


@pytest.fixture
def cleanup(engine: Engine) -> Iterator[list[int]]:
    """Account and user IDs to delete after the test; deleting them cascades to the rest."""
    ids: list[int] = []
    yield ids
    with engine.begin() as connection:
        connection.execute(delete(Account).where(Account.id.in_(ids)))
        connection.execute(delete(GithubUser).where(GithubUser.id.in_(ids)))


# A path runs on its own connection and opens its own transactions.
type Path = Callable[[sessionmaker[Session]], object]
# A statement prefix, or a test of each statement and its parameters.
type Pause = str | Callable[[str, Any], bool]


def _transaction(work: Callable[[Session], object]) -> Path:
    def run(sessions: sessionmaker[Session]) -> None:
        with sessions.begin() as session:
            work(session)

    return run


def _delete_my_data(user_id: int) -> Path:
    return _transaction(lambda session: retention.delete_my_data(session, user_id))


def _race(engine: Engine, paused: Path, pause_before: Pause, *others: Path) -> None:
    """Pause ``paused`` before ``pause_before``, start each of ``others``, then release ``paused``.

    Every path waits at most 5 seconds for a lock, so a wait that is not a deadlock fails too.
    """
    reached, release = threading.Event(), threading.Event()
    matches = (
        pause_before
        if callable(pause_before)
        else lambda statement, _: statement.startswith(pause_before)
    )

    def pause(
        _connection: Connection, _cursor: Any, statement: str, parameters: Any, *_: Any
    ) -> None:
        if not reached.is_set() and matches(statement, parameters):
            reached.set()
            assert release.wait(timeout=10)

    def run(path: Path, listen: bool) -> None:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET lock_timeout = '5s'")
            connection.commit()
            if listen:
                event.listen(connection, "before_cursor_execute", pause)
            try:
                path(sessionmaker(bind=connection))
            finally:
                # The connection goes back to the pool: the next test starts without the limit.
                connection.rollback()
                connection.exec_driver_sql("RESET lock_timeout")
                connection.commit()

    with ThreadPoolExecutor(max_workers=1 + len(others)) as pool:
        running = [pool.submit(run, paused, True)]
        assert reached.wait(timeout=10)
        for other in others:
            running.append(pool.submit(run, other, False))
            # Long enough for it to take every lock it can and wait for the next one.
            time.sleep(0.5)
        release.set()
        for future in running:
            future.result(timeout=15)


def test_a_sign_in_and_delete_my_data_of_a_user_with_a_personal_installation(
    engine: Engine, cleanup: list[int]
) -> None:
    # Paused before it stores the account: by then a sign-in holds whatever it locks first.
    user_id = 7941
    cleanup.append(user_id)
    installation = _installation(4941, user_id, "User")
    repository_id = _store(engine, installation, user_id)
    listed = ListedRepository(
        installation.id, schemas.Repository(id=repository_id, name="app", private=True), True
    )
    sign_in = SignIn(user_id, f"example-{user_id}", (installation,), (listed,), 0)

    _race(
        engine,
        lambda s: store_sign_in(s, sign_in, NOW, None),
        "INSERT INTO account",
        _delete_my_data(user_id),
    )

    with Session(engine) as check:
        assert check.get_one(Account, user_id).data_deleted_at is not None
        # Delete my data waited for the sign-in, so it deleted what the sign-in stored too.
        assert check.get(GithubUser, user_id) is None


def test_a_sign_in_and_delete_my_data_of_an_organisation_member(
    engine: Engine, cleanup: list[int]
) -> None:
    # No personal installation, so Delete my data locks no repository or account. Paused after
    # the user row is stored and before the user's lists are replaced.
    user_id, organisation_id = 7942, 7943
    cleanup.extend([user_id, organisation_id])
    installation = _installation(4943, organisation_id, "Organization")
    repository_id = _store(engine, installation, user_id)
    listed = ListedRepository(
        installation.id, schemas.Repository(id=repository_id, name="app", private=True), False
    )
    sign_in = SignIn(user_id, f"example-{user_id}", (installation,), (listed,), 0)

    _race(
        engine,
        lambda s: store_sign_in(s, sign_in, NOW, None),
        "DELETE FROM user_installation",
        _delete_my_data(user_id),
    )

    with Session(engine) as check:
        assert check.get(GithubUser, user_id) is None
        # The organisation's data stays.
        assert check.scalar(select(Repository.id).where(Repository.id == repository_id))


def test_an_email_choice_save_and_delete_my_data(engine: Engine, cleanup: list[int]) -> None:
    # Paused after the save locked its choice's row and before its consent row, whose foreign key
    # takes FOR KEY SHARE on the user row. Delete my data holds the user row meanwhile and waits
    # for the choice's row.
    user_id = 7944
    cleanup.append(user_id)
    with engine.begin() as connection:
        connection.execute(
            insert(GithubUser).values(id=user_id, login=f"example-{user_id}", last_sign_in_at=NOW)
        )
        connection.execute(
            insert(EmailPreference).values(
                user_id=user_id, kind="usage_warnings", enabled=True, updated_at=NOW
            )
        )

    def save(sessions: sessionmaker[Session]) -> None:
        with sessions.begin() as session:
            assert set_choice(session, user_id, "usage_warnings", False, NOW)

    _race(engine, save, "INSERT INTO email_consent", _delete_my_data(user_id))

    with Session(engine) as check:
        assert check.get(GithubUser, user_id) is None


def _organisation(
    engine: Engine, user_id: int, organisation_id: int
) -> tuple[schemas.Installation, int]:
    """Store an organisation's installation with one repository the user can see; return both."""
    installation = _installation(organisation_id - 3000, organisation_id, "Organization")
    return installation, _store(engine, installation, user_id)


def _sign_in(
    user_id: int, installations: tuple[schemas.Installation, ...], *listed: tuple[int, int]
) -> SignIn:
    """A sign-in listing these installations and (installation ID, repository ID) pairs."""
    repositories = tuple(
        ListedRepository(i, schemas.Repository(id=r, name="app", private=True), False)
        for i, r in listed
    )
    return SignIn(user_id, f"example-{user_id}", installations, repositories, 0)


def _signs_in(sign_in: SignIn, old_session_id: str | None = None) -> Path:
    def run(sessions: sessionmaker[Session]) -> None:
        store_sign_in(sessions, sign_in, NOW, old_session_id)

    return run


def test_an_uninstall_and_a_sign_in_listing_its_repository(
    engine: Engine, cleanup: list[int]
) -> None:
    # Paused after the user's access to the repository is deleted and before the repository.
    user_id, organisation_id = 7951, 7952
    cleanup.extend([user_id, organisation_id])
    installation, repository_id = _organisation(engine, user_id, organisation_id)
    sign_in = _sign_in(user_id, (installation,), (installation.id, repository_id))

    _race(
        engine,
        _transaction(lambda session: retention.uninstall(session, installation.id)),
        "DELETE FROM repository",
        _signs_in(sign_in),
    )

    with Session(engine) as check:
        # The sign-in waited for the uninstall, then stored GitHub's list again.
        assert check.get(Repository, repository_id) is not None


def test_an_uninstall_and_a_sign_in_listing_only_its_installation(
    engine: Engine, cleanup: list[int]
) -> None:
    # Paused before the account, the uninstall's last row. The sign-in stores the account before
    # it stores the installation the uninstall deleted.
    user_id, organisation_id = 7953, 7954
    cleanup.extend([user_id, organisation_id])
    installation, _ = _organisation(engine, user_id, organisation_id)

    _race(
        engine,
        _transaction(lambda session: retention.uninstall(session, installation.id)),
        "DELETE FROM account",
        _signs_in(_sign_in(user_id, (installation,))),
    )

    with Session(engine) as check:
        assert check.get(Installation, installation.id) is not None


def test_an_uninstall_and_a_sign_in_of_a_user_who_lost_access(
    engine: Engine, cleanup: list[int]
) -> None:
    # The sign-in lists nothing, so it locks no repository or account; it deletes the user's
    # installation list and then their repository list. Paused before the uninstall deletes the
    # installation list.
    user_id, organisation_id = 7955, 7956
    cleanup.extend([user_id, organisation_id])
    installation, _ = _organisation(engine, user_id, organisation_id)

    _race(
        engine,
        _transaction(lambda session: retention.uninstall(session, installation.id)),
        "DELETE FROM user_installation",
        _signs_in(_sign_in(user_id, ())),
    )

    with Session(engine) as check:
        assert check.get(Installation, installation.id) is None


def test_a_removal_and_a_job_of_the_removed_repository(engine: Engine, cleanup: list[int]) -> None:
    # Paused after the removal deleted the repository's jobs and before its access lists.
    user_id, organisation_id = 7957, 7958
    cleanup.extend([user_id, organisation_id])
    installation, repository_id = _organisation(engine, user_id, organisation_id)
    job = schemas.WorkflowJobCompleted(
        installation=schemas.InstallationRef(id=installation.id),
        repository=schemas.Repository(id=repository_id, name="app", private=True),
        workflow_job=schemas.WorkflowJob(
            id=7959,
            run_id=79590,
            run_attempt=1,
            workflow_name="ci",
            labels=("ubuntu-latest",),
            runner_id=7,
            runner_name="GitHub Actions 7",
            runner_group_name="GitHub Actions",
            created_at=NOW,
            started_at=NOW,
            completed_at=NOW,
            conclusion="success",
        ),
    )
    with sessionmaker(bind=engine).begin() as session:
        store_job(session, job)

    _race(
        engine,
        _transaction(
            lambda session: retention.remove_repositories(
                session, installation.id, [repository_id]
            )
        ),
        "DELETE FROM user_repository",
        _transaction(lambda session: store_job(session, job)),
    )

    with Session(engine) as check:
        # The redelivered job came after the removal, so it stored the repository again.
        assert check.get(Job, (job.workflow_job.id, 1)) is not None


def test_two_sign_ins_and_a_stale_installation(engine: Engine, cleanup: list[int]) -> None:
    # The second organisation was installed again and its "deleted" delivery was lost, so its old
    # installation and repository are still stored. The first sign-in stores the first account,
    # then uninstalls the old installation; paused as the uninstall starts. The second lists the
    # old repository.
    first_user, second_user, first_id, second_id = 7961, 7962, 7963, 7964
    cleanup.extend([first_user, second_user, first_id, second_id])
    first, _ = _organisation(engine, first_user, first_id)
    stale, repository_id = _organisation(engine, second_user, second_id)
    again = _installation(stale.id + 1, second_id, "Organization")

    _race(
        engine,
        _signs_in(_sign_in(first_user, (first, again))),
        "SELECT installation.account_id",
        _signs_in(_sign_in(second_user, (first, again), (again.id, repository_id))),
    )

    with Session(engine) as check:
        assert check.get(Installation, stale.id) is None
        assert check.get_one(Repository, repository_id).installation_id == again.id


def test_the_daily_purge_and_a_sign_in_with_an_expired_session(
    engine: Engine, cleanup: list[int]
) -> None:
    # The sign-in writes the user's email address, then deletes the expired session it came with;
    # paused between the two. The purge deletes expired sessions, then stale addresses.
    user_id = 7965
    cleanup.append(user_id)
    old_session = "example-old-session"
    with engine.begin() as connection:
        connection.execute(
            insert(GithubUser).values(id=user_id, login=f"example-{user_id}", last_sign_in_at=NOW)
        )
        connection.execute(
            insert(UserSession).values(
                id_sha256=session_digest(old_session),
                user_id=user_id,
                created_at=NOW - timedelta(days=40),
                expires_at=NOW - timedelta(days=40),
            )
        )
        connection.execute(
            insert(UserEmail).values(
                user_id=user_id, address="old@example.invalid", read_at=NOW - timedelta(days=40)
            )
        )
    sign_in = replace(_sign_in(user_id, ()), email="new@example.invalid")

    _race(
        engine,
        _signs_in(sign_in, old_session),
        "DELETE FROM session",
        _transaction(lambda session: retention.run_purges(session, NOW)),
    )

    with Session(engine) as check:
        # The purge waited for the sign-in, which read the address again: it stays.
        assert check.get_one(UserEmail, user_id).address == "new@example.invalid"


def _stored(engine: Engine, installation: schemas.Installation, *repository_ids: int) -> None:
    """Store an installation, its account and these repositories, in this physical order."""
    with engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=installation.account.id,
                login=installation.account.login,
                type=installation.account.type,
                github_plan=schemas.DEFAULT_PLAN[installation.account.type],
            )
        )
        connection.execute(
            insert(Installation).values(
                id=installation.id, account_id=installation.account.id, created_at=NOW
            )
        )
        for repository_id in repository_ids:
            connection.execute(
                insert(Repository).values(
                    id=repository_id, installation_id=installation.id, name="app", private=True
                )
            )


@pytest.fixture
def github_app(rsa_key: RSAKey) -> Iterator[tuple[FakeGithub, GithubApp]]:
    """A fake api.github.com and the real App client that calls it."""
    github = FakeGithub()
    ticks = iter(range(10**6))
    app = GithubApp(
        "Iv23.example",
        rsa_key,
        transport=httpx2.MockTransport(github.handle),
        clock=lambda: fakes.NOW,
        monotonic=lambda: float(next(ticks)),
        sleep=lambda _: None,
    )
    yield github, app
    app.close()


def _lists(github: FakeGithub, *installations: schemas.Installation) -> None:
    github.installations = [
        installation_item(i.id, kind=i.account.type, account_id=i.account.id)
        for i in installations
    ]


def _reconciles(app: GithubApp, completes: list[bool] | None = None) -> Path:
    """Run the reconciliation; record in ``completes`` whether it stored every listed item."""

    def run(sessions: sessionmaker[Session]) -> None:
        complete, _ = reconcile.reconcile(sessions, app, NOW)
        if completes is not None:
            completes.append(complete)

    return run


def _inserts(table: str, row_id: int) -> Callable[[str, Any], bool]:
    """Match the insert or upsert of one row."""
    return lambda statement, parameters: (
        statement.startswith(f"INSERT INTO {table} ") and parameters.get("id") == row_id
    )


def test_the_reconciliation_and_a_sign_in_storing_new_repositories(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # GitHub lists two new repositories in another order than their IDs. Both paths insert them;
    # an insert waits for another's uncommitted row with the same key. Paused before the second.
    github, app = github_app
    user_id, organisation_id, first, second = 7971, 7972, 7973, 7974
    cleanup.extend([user_id, organisation_id])
    installation = _installation(4972, organisation_id, "Organization")
    _stored(engine, installation)
    _lists(github, installation)
    github.repositories = {installation.id: [repository_item(second), repository_item(first)]}
    sign_in = _sign_in(
        user_id, (installation,), (installation.id, first), (installation.id, second)
    )

    _race(engine, _reconciles(app), _inserts("repository", first), _signs_in(sign_in))

    with Session(engine) as check:
        assert check.get(Repository, first) is not None


def test_the_reconciliation_removing_a_repository_and_delete_my_data(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # GitHub no longer lists the lower repository. Paused at the first lock that takes it: the
    # reconciliation's own, before it stores anything, or, without that, the removal's, after
    # it stored the higher one.
    github, app = github_app
    user_id, lower, higher = 7975, 7976, 7977
    cleanup.append(user_id)
    installation = _installation(4975, user_id, "User")
    _stored(engine, installation, lower, higher)
    _lists(github, installation)
    github.repositories = {installation.id: [repository_item(higher)]}

    _race(
        engine,
        _reconciles(app),
        lambda statement, parameters: (
            statement.startswith("SELECT repository.id")
            and "FOR UPDATE" in statement
            and lower in parameters.values()
        ),
        _delete_my_data(user_id),
    )

    with Session(engine) as check:
        assert check.get(Repository, lower) is None


def test_the_reconciliation_and_a_sign_in_storing_new_accounts(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # GitHub lists two new installations in another order than their accounts' IDs. Paused before
    # the second account is inserted.
    github, app = github_app
    user_id, first, second = 7978, 7979, 7980
    cleanup.extend([user_id, first, second])
    installations = (
        _installation(4980, second, "Organization"),
        _installation(4979, first, "Organization"),
    )
    _lists(github, *installations)

    _race(
        engine,
        _reconciles(app),
        _inserts("account", first),
        _signs_in(_sign_in(user_id, installations)),
    )

    with Session(engine) as check:
        assert check.get(Account, first) is not None


def test_the_reconciliation_uninstalling_a_higher_account_and_a_sign_in(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # The higher account's installation is gone; the sign-in still lists it. Paused after the
    # uninstall and before the lower account is stored.
    github, app = github_app
    user_id, lower, higher = 7981, 7982, 7983
    cleanup.extend([user_id, lower, higher])
    listed = _installation(4982, lower, "Organization")
    gone = _installation(4983, higher, "Organization")
    _stored(engine, listed)
    _stored(engine, gone)
    _lists(github, listed)

    _race(
        engine,
        _reconciles(app),
        _inserts("account", lower),
        _signs_in(_sign_in(user_id, (listed, gone))),
    )

    with Session(engine) as check:
        assert check.get(Account, lower) is not None


def test_the_reconciliation_uninstalling_two_installations_and_a_sign_in(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # Two installations are gone; the sign-in still lists the first and the second's repository.
    # Paused as the second uninstall starts, after the first one locked its account.
    github, app = github_app
    user_id, kept_id, first_id, second_id = 7984, 7985, 7986, 7987
    cleanup.extend([user_id, kept_id, first_id, second_id])
    kept = _installation(4985, kept_id, "Organization")
    first = _installation(4986, first_id, "Organization")
    second = _installation(4987, second_id, "Organization")
    _stored(engine, kept)
    _stored(engine, first, 7988)
    _stored(engine, second, 7989)
    _lists(github, kept)

    _race(
        engine,
        _reconciles(app),
        lambda statement, parameters: (
            statement.startswith("SELECT installation.account_id")
            and second.id in parameters.values()
        ),
        _signs_in(_sign_in(user_id, (first, second), (second.id, 7989))),
    )

    with Session(engine) as check:
        # The sign-in waited for both uninstalls, then stored its own list again.
        assert check.get(Installation, first.id) is not None


def test_the_backfill_queue_delete_my_data_and_a_sign_in(
    engine: Engine, cleanup: list[int]
) -> None:
    # The higher repository is stored first, so the queue's foreign-key checks lock it first. The
    # sign-in holds it (FOR NO KEY UPDATE), so Delete my data, holding the lower one, waits for it;
    # the queue's FOR KEY SHARE does not wait for the sign-in.
    user_id, lower, higher = 7990, 7991, 7992
    cleanup.append(user_id)
    installation = _stored_higher_first(engine, user_id, lower, higher)

    _race(
        engine,
        _signs_in(_sign_in(user_id, (installation,), (installation.id, higher))),
        "INSERT INTO account",
        _delete_my_data(user_id),
        _transaction(backfill.queue),
    )

    with Session(engine) as check:
        # Delete my data marked both backfilled before the queue read them.
        assert not check.scalars(select(Backfill).where(Backfill.repository_id == lower)).all()


def test_a_backfill_finish_and_delete_my_data(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # The repository has no runs, so the pass finishes: it deletes its queued row, then marks the
    # repository backfilled; paused between the two.
    github, app = github_app
    user_id, repository_id = 7993, 7994
    cleanup.append(user_id)
    installation = _installation(4993, user_id, "User")
    _stored(engine, installation, repository_id)
    with engine.begin() as connection:
        connection.execute(insert(Backfill).values(repository_id=repository_id))
    github.override = lambda request: (
        httpx2.Response(200, json={"total_count": 0, "workflow_runs": []})
        if request.url.path.endswith("/actions/runs")
        else None
    )

    _race(
        engine,
        lambda sessions: backfill.run_next(sessions, app, NOW),
        "UPDATE repository",
        _delete_my_data(user_id),
    )

    with Session(engine) as check:
        assert check.get_one(Account, user_id).data_deleted_at is not None


def test_a_sign_in_uninstalling_a_stale_installation_a_requeue_and_delete_my_data(
    engine: Engine, cleanup: list[int]
) -> None:
    # The sign-in holds the stale organisation's repository and the user's higher personal one,
    # paused as its uninstall of the stale installation starts. Delete my data holds the lower
    # personal one and waits for the higher. A re-queue of the stale and the lower one takes FOR
    # KEY SHARE: on the stale one only if the sign-in's lock lets it, then waits for the lower.
    user_id, organisation_id, stale_repository, lower, higher = 7995, 7996, 7997, 7998, 7999
    cleanup.extend([user_id, organisation_id])
    personal = _installation(4995, user_id, "User")
    stale = _installation(4996, organisation_id, "Organization")
    again = _installation(4997, organisation_id, "Organization")
    _stored(engine, personal, lower, higher)
    _stored(engine, stale, stale_repository)
    sign_in = _sign_in(user_id, (personal, again), (personal.id, higher))

    _race(
        engine,
        _signs_in(sign_in),
        "SELECT installation.account_id",
        _delete_my_data(user_id),
        _transaction(lambda session: backfill.requeue(session, [stale_repository, lower])),
    )

    with Session(engine) as check:
        assert check.get(Installation, stale.id) is None


def _job(
    installation_id: int, repository_id: int, job_id: int, created_at: datetime
) -> schemas.WorkflowJobCompleted:
    return schemas.WorkflowJobCompleted(
        installation=schemas.InstallationRef(id=installation_id),
        repository=schemas.Repository(id=repository_id, name="app", private=True),
        workflow_job=schemas.WorkflowJob(
            id=job_id,
            run_id=job_id * 10,
            run_attempt=1,
            workflow_name="ci",
            labels=("ubuntu-latest",),
            runner_id=7,
            runner_name="GitHub Actions 7",
            runner_group_name="GitHub Actions",
            created_at=created_at,
            started_at=created_at,
            completed_at=created_at,
            conclusion="success",
        ),
    )


def test_the_daily_purge_and_a_backfill_storing_old_jobs(
    engine: Engine, cleanup: list[int]
) -> None:
    # Two jobs past the purge's cutoff, the older stored first, so the purge's DELETE locks it
    # first in either scan order. One transaction stores them again, the newer first, as a run's
    # jobs; paused before the older. No path stores jobs this old: the two upserts stand in for
    # two DELETEs that scan the same jobs in opposite orders, which no test can pause between rows.
    user_id, repository_id = 8000, 8001
    cleanup.append(user_id)
    installation = _installation(5000, user_id, "User")
    _stored(engine, installation, repository_id)
    cutoff = NOW - retention.JOB_RETENTION
    older = _job(installation.id, repository_id, 8002, cutoff - timedelta(days=2))
    newer = _job(installation.id, repository_id, 8003, cutoff - timedelta(days=1))
    with sessionmaker(bind=engine).begin() as session:
        store_job(session, older)
        store_job(session, newer)

    def stores(session: Session) -> None:
        store_job(session, newer)
        store_job(session, older)

    _race(
        engine,
        _transaction(stores),
        _inserts("job", older.workflow_job.id),
        _transaction(lambda session: retention.run_purges(session, NOW)),
    )

    with Session(engine) as check:
        # The purge waited for the stores, then deleted both.
        assert not check.scalars(select(Job).where(Job.repository_id == repository_id)).all()


def test_the_backfill_queue_and_a_requeue_of_the_same_repository(
    engine: Engine, cleanup: list[int]
) -> None:
    # The re-queue inserts the repository's row; paused before it commits. The queue cannot see
    # the row, so it inserts one too and waits for the re-queue.
    user_id, repository_id = 8004, 8005
    cleanup.append(user_id)
    installation = _installation(5004, user_id, "User")
    _stored(engine, installation, repository_id)
    queued: list[int] = []

    def requeues(session: Session) -> None:
        backfill.requeue(session, [repository_id])
        session.execute(text("SELECT 'requeued'"))

    _race(
        engine,
        _transaction(requeues),
        "SELECT 'requeued'",
        _transaction(lambda session: queued.append(backfill.queue(session))),
    )

    # The queue skipped the row the re-queue committed.
    assert queued == [0]


def _requeues_lower_then_higher(lower: int, higher: int) -> tuple[Path, Pause]:
    """Re-queue the lower repository, then the higher; paused before the higher one's insert."""

    def requeues(session: Session) -> None:
        backfill.requeue(session, [lower])
        backfill.requeue(session, [higher])

    def before_higher(statement: str, parameters: Any) -> bool:
        return statement.startswith("INSERT INTO backfill") and higher in parameters.values()

    return _transaction(requeues), before_higher


def _stored_higher_first(
    engine: Engine, user_id: int, lower: int, higher: int
) -> schemas.Installation:
    """Store a personal installation and two repositories, the higher one physically first.

    The repository table is emptied first: Postgres puts a new row wherever it finds free space,
    so after other tests' rows the second can land before the first. An empty table takes them
    on its first page, in insert order.
    """
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE repository CASCADE"))
    installation = _installation(user_id - 3000, user_id, "User")
    _stored(engine, installation, higher, lower)
    with engine.connect() as connection:
        order = connection.scalars(
            select(Repository.id).where(Repository.id.in_([lower, higher])).order_by(text("ctid"))
        ).all()
    assert order == [higher, lower]
    return installation


def test_the_backfill_queue_and_a_requeue_insert_in_id_order(
    engine: Engine, cleanup: list[int]
) -> None:
    # The higher repository is stored first, so a queue that inserts in the order it reads takes
    # the higher row first, then waits for the re-queue's lower one.
    user_id, lower, higher = 8006, 8007, 8008
    cleanup.append(user_id)
    _stored_higher_first(engine, user_id, lower, higher)
    queued: list[int] = []

    _race(
        engine,
        *_requeues_lower_then_higher(lower, higher),
        _transaction(lambda session: queued.append(backfill.queue(session))),
    )

    # The queue waited for the re-queue and skipped both rows it committed.
    assert queued == [0]


def test_two_requeues_insert_in_id_order(engine: Engine, cleanup: list[int]) -> None:
    # The same with a re-queue of both. Without index scans its select reads in physical order,
    # a plan Postgres may choose: nothing else puts the rows in ID order.
    user_id, lower, higher = 8009, 8010, 8011
    cleanup.append(user_id)
    _stored_higher_first(engine, user_id, lower, higher)

    def requeues_both(session: Session) -> None:
        session.execute(text("SET LOCAL enable_indexscan = off"))
        session.execute(text("SET LOCAL enable_bitmapscan = off"))
        backfill.requeue(session, [lower, higher])

    _race(engine, *_requeues_lower_then_higher(lower, higher), _transaction(requeues_both))

    with Session(engine) as check:
        generations = check.scalars(
            select(Backfill.generation)
            .where(Backfill.repository_id.in_([lower, higher]))
            .order_by(Backfill.repository_id)
        ).all()
    # The second re-queue waited for the first and raised both rows' generation.
    assert generations == [1, 1]


def test_the_reconciliation_storing_a_new_lower_account_and_a_sign_in(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # GitHub lists a new account's installation and a stored higher account's. The new account
    # was installed again meanwhile: the sign-in lists its newer installation, and the higher one.
    # Paused before the reconciliation inserts the new account.
    github, app = github_app
    user_id, new, higher = 8012, 8013, 8014
    cleanup.extend([user_id, new, higher])
    stored = _installation(5014, higher, "Organization")
    listed = _installation(5013, new, "Organization")
    again = _installation(5015, new, "Organization")
    _stored(engine, stored)
    _lists(github, listed, stored)

    _race(
        engine,
        _reconciles(app),
        _inserts("account", new),
        _signs_in(_sign_in(user_id, (again, stored))),
    )

    with Session(engine) as check:
        # The reconciliation stored the new account first, under its installation lock; the
        # sign-in waited for it, then uninstalled its installation in the lock order.
        assert check.get(Installation, again.id) is not None
        assert check.get(Installation, listed.id) is None
        assert check.get(Installation, stored.id) is not None


def test_the_reconciliation_and_a_sign_in_installing_a_new_account_again(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # GitHub lists a new account's installation; the sign-in stores a newer one for the same
    # account and is paused before it commits.
    github, app = github_app
    user_id, new = 8015, 8016
    cleanup.extend([user_id, new])
    listed = _installation(5016, new, "Organization")
    again = _installation(5017, new, "Organization")
    _lists(github, listed)
    completes: list[bool] = []

    _race(
        engine,
        _signs_in(_sign_in(user_id, (again,))),
        "INSERT INTO github_user",
        _reconciles(app, completes),
    )

    with Session(engine) as check:
        assert check.get(Installation, again.id) is not None
        assert check.get(Installation, listed.id) is None
    assert completes == [False]


def _installation_created(installation: schemas.Installation) -> Callable[[Session], None]:
    """The webhook's ``installation.created`` handler for this installation."""
    payload = {"installation": installation.model_dump(mode="json")}
    return lambda session: HANDLERS[("installation", "created")](session, payload)


def test_a_sign_in_and_the_reconciliation_storing_the_same_new_account(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # The reconciliation stores a new account's installation; paused before its installation
    # insert. The account was installed again: the sign-in lists the newer installation.
    github, app = github_app
    user_id, new = 8100, 8101
    cleanup.extend([user_id, new])
    listed = _installation(5100, new, "Organization")
    again = _installation(5101, new, "Organization")
    _lists(github, listed)

    _race(
        engine,
        _reconciles(app),
        _inserts("installation", listed.id),
        _signs_in(_sign_in(user_id, (again,))),
    )

    with Session(engine) as check:
        # The sign-in waited for the reconciliation, then uninstalled its installation.
        assert check.get(Installation, again.id) is not None
        assert check.get(Installation, listed.id) is None


def test_a_sign_in_and_a_webhook_storing_the_same_new_account(
    engine: Engine, cleanup: list[int]
) -> None:
    # The same with the webhook's installation.created, paused before its installation insert.
    user_id, new = 8102, 8103
    cleanup.extend([user_id, new])
    created = _installation(5102, new, "Organization")
    again = _installation(5103, new, "Organization")

    _race(
        engine,
        _transaction(_installation_created(created)),
        _inserts("installation", created.id),
        _signs_in(_sign_in(user_id, (again,))),
    )

    with Session(engine) as check:
        assert check.get(Installation, again.id) is not None
        assert check.get(Installation, created.id) is None


def test_a_sign_in_uninstalling_an_installation_stored_after_its_read(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # The sign-in lists a stored lower account with its repository, and a new account; paused
    # before it reads the new account's stale installation, holding the repository and the lower
    # account. A webhook then stores another installation for the new account, and a job stores a
    # lower repository under it. The reconciliation's repository step for that installation, whose
    # list holds both repositories (the higher one moved to it), takes the lower one and waits for
    # the higher. The step runs on its own: a whole run would first wait for the accounts.
    github, app = github_app
    user_id, lower, new = 8104, 8106, 8107
    late_repository, repository_id = 8108, 8109
    cleanup.extend([user_id, lower, new])
    stored = _installation(5106, lower, "Organization")
    again = _installation(5107, new, "Organization")
    created = _installation(5108, new, "Organization")
    _stored(engine, stored, repository_id)
    github.repositories = {
        created.id: [repository_item(late_repository), repository_item(repository_id)]
    }

    def webhook(session: Session) -> None:
        _installation_created(created)(session)
        store_job(session, _job(created.id, late_repository, 8110, NOW))

    _race(
        engine,
        _signs_in(_sign_in(user_id, (stored, again), (stored.id, repository_id))),
        lambda statement, parameters: (
            statement.startswith("SELECT installation.id")
            and "installation.account_id = " in statement
            and new in parameters.values()
        ),
        _transaction(webhook),
        lambda sessions: reconcile._reconcile_repositories(sessions, app, created.id, NOW),
    )

    with Session(engine) as check:
        # The webhook waited for the sign-in and stored its installation after it.
        assert check.get(Installation, created.id) is not None
        assert check.get(Installation, again.id) is None
        assert check.get_one(Repository, late_repository).installation_id == created.id


def test_a_sign_in_and_the_reconciliation_installing_an_account_again(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # An account was installed again: GitHub lists its new installation and answers 404 for the
    # stored one, which the reconciliation uninstalls; paused just after. The sign-in lists a
    # stored lower account and a newer installation still: it reads the stored one as stale and
    # waits for its repository. It must uninstall only what it read and locked first.
    github, app = github_app
    user_id, lower, account_id, repository_id = 8115, 8116, 8117, 8118
    cleanup.extend([user_id, lower, account_id])
    kept = _installation(5116, lower, "Organization")
    old = _installation(5117, account_id, "Organization")
    listed = _installation(5118, account_id, "Organization")
    newer = _installation(5119, account_id, "Organization")
    _stored(engine, kept)
    _stored(engine, old, repository_id)
    _lists(github, kept, listed)
    uninstalled: list[int] = []

    def signs_in(sessions: sessionmaker[Session]) -> None:
        def record(*args: Any) -> None:
            statement, parameters = args[2], args[3]
            if statement.startswith("SELECT installation.account_id"):
                uninstalled.extend(parameters.values())

        event.listen(sessions.kw["bind"], "before_cursor_execute", record)
        store_sign_in(sessions, _sign_in(user_id, (kept, newer)), NOW, None)

    _race(
        engine,
        _reconciles(app),
        "SELECT installation.id, installation.account_id",
        signs_in,
    )

    # The reconciliation uninstalled the old installation; the sign-in had nothing more to.
    assert uninstalled == []
    with Session(engine) as check:
        assert check.get(Installation, newer.id) is not None
        assert check.get(Installation, listed.id) is None


def test_a_sign_in_storing_new_repositories_listed_out_of_id_order(
    engine: Engine, cleanup: list[int], github_app: tuple[FakeGithub, GithubApp]
) -> None:
    # GitHub lists two new repositories in another order than their IDs, to both paths. The
    # reconciliation inserts the lower one and is paused before the higher. The sign-in's one
    # pipelined call must insert the lower first too, and wait for it, rather than hold the higher.
    github, app = github_app
    user_id, organisation_id, first, second = 8111, 8112, 8113, 8114
    cleanup.extend([user_id, organisation_id])
    installation = _installation(5112, organisation_id, "Organization")
    _stored(engine, installation)
    _lists(github, installation)
    github.repositories = {installation.id: [repository_item(second), repository_item(first)]}
    sign_in = _sign_in(
        user_id, (installation,), (installation.id, second), (installation.id, first)
    )

    _race(engine, _reconciles(app), _inserts("repository", second), _signs_in(sign_in))

    with Session(engine) as check:
        assert check.get(Repository, second) is not None
