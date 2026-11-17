"""A sign-in's repositories: one pipelined call, today's per-row upsert, and the later entry wins.

Each test runs in a transaction rolled back after it (``tests/conftest.py``).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, event
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.service import ListedRepository, SignIn, store_sign_in
from burnbird.github import schemas
from burnbird.models import Repository

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
USER = 9100


def _installation(installation_id: int, account_id: int) -> schemas.Installation:
    return schemas.Installation.model_validate(
        {
            "id": installation_id,
            "account": {
                "id": account_id,
                "login": f"example-{account_id}",
                "type": "Organization",
            },
            "created_at": NOW,
        }
    )


def _sign_in(
    installations: tuple[schemas.Installation, ...], *listed: tuple[int, int, str, bool]
) -> SignIn:
    """A sign-in listing these (installation ID, repository ID, name, private) entries."""
    return SignIn(
        USER,
        f"example-{USER}",
        installations,
        tuple(
            ListedRepository(i, schemas.Repository(id=r, name=name, private=private), False)
            for i, r, name, private in listed
        ),
        0,
    )


def test_a_sign_in_sends_its_repositories_in_one_pipelined_call(
    connection: Connection, sessions: sessionmaker[Session]
) -> None:
    # One INSERT executed with a list of rows: SQLAlchemy hands it to the driver's
    # executemany, which psycopg pipelines, rather than one round trip per repository.
    installation = _installation(9201, 9101)
    listed = [(installation.id, 9_300_000 + n, f"app-{n}", True) for n in range(1000)]
    inserts: list[bool] = []

    def record(*args: Any) -> None:
        statement, executemany = args[2], args[5]
        if statement.startswith("INSERT INTO repository"):
            inserts.append(executemany)

    event.listen(connection, "before_cursor_execute", record)
    store_sign_in(sessions, _sign_in((installation,), *listed), NOW, None)
    event.remove(connection, "before_cursor_execute", record)

    assert inserts == [True]
    with sessions() as session:
        assert session.get(Repository, 9_300_999) is not None


def test_a_repository_github_lists_twice_is_stored_once(sessions: sessionmaker[Session]) -> None:
    # Each listed entry is its own upsert, in the order listed: the later one wins, as before.
    installation = _installation(9201, 9101)
    sign_in = _sign_in(
        (installation,),
        (installation.id, 9_300_001, "first", True),
        (installation.id, 9_300_001, "second", False),
    )

    store_sign_in(sessions, sign_in, NOW, None)

    with sessions() as session:
        stored = session.get_one(Repository, 9_300_001)
        assert (stored.name, stored.private) == ("second", False)


def test_a_later_sign_in_updates_a_repositorys_installation_name_and_privacy(
    sessions: sessionmaker[Session],
) -> None:
    first, second = _installation(9201, 9101), _installation(9202, 9102)
    store_sign_in(
        sessions, _sign_in((first, second), (first.id, 9_300_001, "old", True)), NOW, None
    )

    store_sign_in(
        sessions, _sign_in((first, second), (second.id, 9_300_001, "new", False)), NOW, None
    )

    with sessions() as session:
        stored = session.get_one(Repository, 9_300_001)
        assert (stored.installation_id, stored.name, stored.private) == (second.id, "new", False)
