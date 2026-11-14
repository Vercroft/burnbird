"""A real Postgres for the storage tests, migrated once, with every test rolled back.

The URL comes from BURNBIRD_TEST_DATABASE_URL. Without it the storage tests fail rather than skip,
so a run without a database cannot pass. The guard refuses any database not named burnbird_test.
"""

import functools
import logging
import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from joserfc.jwk import RSAKey
from sqlalchemy import Connection, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.config import AuthSettings
from burnbird.database import DATABASE_URL_VARIABLE, make_engine

TEST_URL_VARIABLE = "BURNBIRD_TEST_DATABASE_URL"
TEST_DATABASE = "burnbird_test"
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(TEST_URL_VARIABLE)
    if not url:
        pytest.fail(f"set {TEST_URL_VARIABLE} to a throwaway Postgres (see CONTRIBUTING.md)")
    # The guard: never touch any database but the throwaway one.
    if make_url(url).database != TEST_DATABASE:
        pytest.fail(f"{TEST_URL_VARIABLE} must name the database {TEST_DATABASE}")
    return url


def _migrate(database_url: str, revision: str, *, up: bool) -> None:
    # The repository's own Alembic configuration, as the command line reads it.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(DATABASE_URL_VARIABLE, database_url)
        config = Config(toml_file=str(PYPROJECT))
        if up:
            command.upgrade(config, revision)
        else:
            command.downgrade(config, revision)


@pytest.fixture(scope="session")
def upgrade(database_url: str) -> Callable[[str], None]:
    return functools.partial(_migrate, database_url, up=True)


@pytest.fixture(scope="session")
def downgrade(database_url: str) -> Callable[[str], None]:
    return functools.partial(_migrate, database_url, up=False)


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    _migrate(database_url, "head", up=True)
    engine = make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        transaction.rollback()


@pytest.fixture
def session(connection: Connection) -> Iterator[Session]:
    # Commits inside a test become savepoints; the outer transaction is rolled back after it.
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        yield session


@pytest.fixture
def sessions(connection: Connection) -> sessionmaker[Session]:
    # Commits inside a test become savepoints of the test's rolled-back transaction.
    return sessionmaker(bind=connection, join_transaction_mode="create_savepoint")


# The test client's own origin: cookies marked Secure are sent back only over https.
ORIGIN = "https://burnbird.test"


@pytest.fixture
def auth_settings() -> AuthSettings:
    """Sign-in settings with random secrets, made for each test."""
    return AuthSettings(
        client_id="Iv1.example",
        client_secret=secrets.token_hex(20),
        state_key=secrets.token_urlsafe(48),
        origin=ORIGIN,
    )


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Put the logging set-up back after a test that configures it as a command does."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    floors = {name: logging.getLogger(name).level for name in ("authlib",)}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, floor in floors.items():
        logging.getLogger(name).setLevel(floor)


@pytest.fixture(scope="session")
def rsa_key() -> RSAKey:
    """One RSA key for the App, made once per run: GitHub's App keys are 2048-bit RSA."""
    return RSAKey.generate_key(2048)
