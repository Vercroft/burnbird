"""The database connection: its URL comes from the environment, and every call has a time limit."""

import os
from typing import Final

from sqlalchemy import Engine, create_engine

DATABASE_URL_VARIABLE: Final = "BURNBIRD_DATABASE_URL"

# Postgres waits forever by default (statement_timeout 0), so both limits are set here.
CONNECT_TIMEOUT_SECONDS: Final = 10
STATEMENT_TIMEOUT_MILLISECONDS: Final = 30_000


class DatabaseUrlMissingError(Exception):
    """The environment variable that holds the database URL is not set."""


def database_url() -> str:
    """Return the database URL from the environment; it is never read from a file."""
    url = os.environ.get(DATABASE_URL_VARIABLE)
    if not url:
        raise DatabaseUrlMissingError(f"set {DATABASE_URL_VARIABLE} to the database URL")
    return url


def make_engine(url: str) -> Engine:
    """Create an engine whose connections and statements time out, and whose errors hide values.

    With ``hide_parameters``, "SQL statement parameters will not be displayed in INFO logging nor
    will they be formatted into the string representation of StatementError objects", so a logged
    traceback never shows a stored value such as a private repository's name (SQLAlchemy 2.1,
    ``create_engine`` parameters).
    """
    return create_engine(
        url,
        hide_parameters=True,
        connect_args={
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MILLISECONDS}",
        },
    )
