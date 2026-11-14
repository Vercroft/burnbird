import secrets

import pytest
from sqlalchemy import Engine, insert
from sqlalchemy.exc import IntegrityError

from burnbird.models import Repository


def test_a_failed_statement_never_shows_its_parameters(engine: Engine) -> None:
    # A private repository's name, planted at run time; the insert fails: no such installation.
    planted = f"private-{secrets.token_hex(8)}"
    with engine.connect() as connection, pytest.raises(IntegrityError) as raised:
        connection.execute(
            insert(Repository).values(id=1, installation_id=1, name=planted, private=True)
        )
    assert planted not in str(raised.value)
    assert "SQL parameters hidden" in str(raised.value)
