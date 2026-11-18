"""The test-database guard: the tests touch only the throwaway database, or one made per worker.

Under pytest-xdist each worker (gw0, gw1, ...) gets its own database, named from the worker and
made and dropped by the tests. Any other name is refused, so a stray value can never point the
tests at a real database.
"""

import pytest

from tests.conftest import database_name, worker_database_url

URL = "postgresql+psycopg://postgres@127.0.0.1:5432/"


@pytest.mark.parametrize(
    ("worker", "name"),
    [
        (None, "burnbird_test"),
        ("", "burnbird_test"),
        ("gw0", "burnbird_test_gw0"),
        ("gw12", "burnbird_test_gw12"),
    ],
)
def test_the_database_is_the_throwaway_one_or_its_worker_s(worker: str | None, name: str) -> None:
    assert database_name(worker) == name


@pytest.mark.parametrize(
    "worker", ["gw", "gwx", "gw1x", "../x", "master", "master2", "gw1; DROP", " gw1"]
)
def test_any_other_worker_name_is_refused(worker: str) -> None:
    with pytest.raises(ValueError, match="refusing"):
        database_name(worker)


def test_a_worker_s_url_names_its_own_database() -> None:
    assert worker_database_url(URL + "burnbird_test", "gw3") == URL + "burnbird_test_gw3"
    assert worker_database_url(URL + "burnbird_test", None) == URL + "burnbird_test"


@pytest.mark.parametrize(
    "database", ["burnbird", "postgres", "burnbird_test_gw0", "burnbird_testx"]
)
def test_the_environment_must_name_the_throwaway_database(database: str) -> None:
    with pytest.raises(ValueError, match="must name the database burnbird_test"):
        worker_database_url(URL + database, None)
