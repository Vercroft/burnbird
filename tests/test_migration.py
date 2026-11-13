from collections.abc import Callable

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect

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
