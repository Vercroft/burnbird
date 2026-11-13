"""Alembic's environment: run the migrations against the database named in the environment."""

from alembic import context

from burnbird.database import database_url, make_engine
from burnbird.models import Base


def run_migrations() -> None:
    """Run the migrations online; offline SQL scripts are not used."""
    if context.is_offline_mode():
        raise RuntimeError("offline mode is not supported; run against a database")
    engine = make_engine(database_url())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
