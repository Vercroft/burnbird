"""A sign-in and Delete my data for the same user, at the same moment, never deadlock.

Two connections need committed rows; each test deletes them again at the end. The sign-in runs on
its own connection and is paused just before one statement, holding the locks it took so far;
Delete my data starts on another connection meanwhile; then the sign-in goes on. Both must finish.
A deadlock would make Postgres abort one of them, and the test fails with its error.
"""

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, delete, event, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.auth.service import ListedRepository, SignIn, store_sign_in
from burnbird.github import schemas
from burnbird.models import (
    Account,
    GithubUser,
    Installation,
    Repository,
    UserInstallation,
    UserRepository,
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


def _race(engine: Engine, sign_in: SignIn, pause_before: str) -> None:
    """Pause the sign-in before ``pause_before``, run Delete my data, then release the sign-in."""
    reached, release = threading.Event(), threading.Event()

    def pause(_connection: Connection, _cursor: Any, statement: str, *_: Any) -> None:
        if statement.startswith(pause_before) and not reached.is_set():
            reached.set()
            assert release.wait(timeout=10)

    def sign_in_now() -> None:
        with engine.connect() as connection:
            event.listen(connection, "before_cursor_execute", pause)
            store_sign_in(sessionmaker(bind=connection), sign_in, NOW, None)

    def delete_mine() -> None:
        with sessionmaker(bind=engine).begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            retention.delete_my_data(session, sign_in.user_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        signing = pool.submit(sign_in_now)
        assert reached.wait(timeout=10)
        deleting = pool.submit(delete_mine)
        # Long enough for Delete my data to take every lock it can and wait for the next one.
        time.sleep(0.5)
        release.set()
        signing.result(timeout=15)
        deleting.result(timeout=15)


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

    _race(engine, sign_in, "INSERT INTO account")

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

    _race(engine, sign_in, "DELETE FROM user_installation")

    with Session(engine) as check:
        assert check.get(GithubUser, user_id) is None
        # The organisation's data stays.
        assert check.scalar(select(Repository.id).where(Repository.id == repository_id))
