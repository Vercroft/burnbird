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

import pytest
from sqlalchemy import Connection, Engine, delete, event, insert, select
from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.auth.service import ListedRepository, SignIn, session_digest, store_sign_in
from burnbird.email.preferences import set_choice
from burnbird.github import backfill, schemas
from burnbird.github.events import store_job
from burnbird.models import (
    Account,
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
