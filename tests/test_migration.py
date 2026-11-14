from collections.abc import Callable

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from burnbird.models import Base

TABLES = {
    "account",
    "github_user",
    "installation",
    "job",
    "label_size",
    "repository",
    "session",
    "user_installation",
    "user_repository",
    "webhook_delivery",
    "worker_task",
}


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_migration_goes_down_and_up_cleanly(
    upgrade: Callable[[str], None], downgrade: Callable[[str], None], engine: Engine
) -> None:
    downgrade("base")
    assert _tables(engine) == set()
    upgrade("head")
    assert _tables(engine) == TABLES
    downgrade("base")
    upgrade("head")
    assert _tables(engine) == TABLES


def test_the_models_match_the_migrated_schema(engine: Engine) -> None:
    assert set(Base.metadata.tables) == TABLES
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_a_user_repository_row_from_before_0005_is_not_admin(
    upgrade: Callable[[str], None], downgrade: Callable[[str], None], engine: Engine
) -> None:
    # Invented rows, stored the way 0004's schema allows (no admin column), then migrated up.
    # They are committed, because a migration runs on its own connection; deleted at the end.
    downgrade("0004")
    try:
        with engine.begin() as connection:
            for statement in (
                "INSERT INTO github_user (id, login, last_sign_in_at) "
                "VALUES (7101, 'example-migrated', now())",
                "INSERT INTO account (id, login, type, github_plan) "
                "VALUES (3101, 'example-migrated-org', 'Organization', "
                "'GitHub Free for organizations')",
                "INSERT INTO installation (id, account_id, created_at) VALUES (4101, 3101, now())",
                "INSERT INTO repository (id, installation_id, name, private) "
                "VALUES (5101, 4101, 'app', true)",
                "INSERT INTO user_repository (user_id, repository_id) VALUES (7101, 5101)",
            ):
                connection.execute(text(statement))
        upgrade("head")
        with engine.connect() as connection:
            admin = connection.scalar(
                text("SELECT admin FROM user_repository WHERE user_id = 7101")
            )
        assert admin is False
    finally:
        upgrade("head")
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM user_repository WHERE user_id = 7101"))
            connection.execute(text("DELETE FROM repository WHERE id = 5101"))
            connection.execute(text("DELETE FROM installation WHERE id = 4101"))
            connection.execute(text("DELETE FROM account WHERE id = 3101"))
            connection.execute(text("DELETE FROM github_user WHERE id = 7101"))
