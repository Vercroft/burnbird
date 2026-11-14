"""Option B: who may see an account's whole numbers, its forecast and its settings.

Invented accounts only. A personal account's own user may; in an organisation, only a user with
admin on every repository of a fully synced installation that has at least one may. Everyone
else may not, and an unknown installation answers like one the user may not see.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from burnbird import retention
from burnbird.auth.dependencies import account_access
from burnbird.github import schemas
from burnbird.github.events import store_repository
from burnbird.models import (
    Account,
    GithubUser,
    Installation,
    Repository,
    UserInstallation,
    UserRepository,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
PERSON, OTHER_PERSON = 7001, 7002
ORG, OTHER_ORG = 3001, 3002
PERSONAL_INSTALLATION, ORG_INSTALLATION, OTHER_INSTALLATION = 4007, 4001, 4002


@pytest.fixture
def world(session: Session) -> Session:
    """A personal account and two organisations, each installed and synced, and two users."""
    session.add_all(
        [
            GithubUser(id=PERSON, login="example-person", last_sign_in_at=NOW),
            GithubUser(id=OTHER_PERSON, login="example-other", last_sign_in_at=NOW),
            Account(id=PERSON, login="example-person", type="User", github_plan="GitHub Free"),
            Account(
                id=ORG,
                login="example-org",
                type="Organization",
                github_plan="GitHub Free for organizations",
            ),
            Account(
                id=OTHER_ORG,
                login="example-other-org",
                type="Organization",
                github_plan="GitHub Free for organizations",
            ),
        ]
    )
    session.flush()
    for installation_id, account_id in (
        (PERSONAL_INSTALLATION, PERSON),
        (ORG_INSTALLATION, ORG),
        (OTHER_INSTALLATION, OTHER_ORG),
    ):
        session.add(Installation(id=installation_id, account_id=account_id, created_at=NOW))
    session.flush()
    # Synced just now by the database's clock, which account_access measures against.
    session.execute(update(Installation).values(repositories_synced_at=func.now()))
    for repository_id, installation_id in (
        (5071, PERSONAL_INSTALLATION),
        (5011, ORG_INSTALLATION),
        (5012, ORG_INSTALLATION),
        (5013, ORG_INSTALLATION),
        (5021, OTHER_INSTALLATION),
    ):
        session.add(
            Repository(id=repository_id, installation_id=installation_id, name="app", private=True)
        )
    session.flush()
    return session


@pytest.fixture
def grant(world: Session) -> Callable[..., None]:
    """List an installation for a user, with the given repositories and their admin flags."""

    def add(user_id: int, installation_id: int, repositories: dict[int, bool]) -> None:
        world.add(UserInstallation(user_id=user_id, installation_id=installation_id))
        for repository_id, admin in repositories.items():
            world.add(UserRepository(user_id=user_id, repository_id=repository_id, admin=admin))
        world.flush()

    return add


EVERY_ORG_REPOSITORY = {5011: True, 5012: True, 5013: True}


def test_the_personal_accounts_own_user_may(world: Session, grant: Callable[..., None]) -> None:
    grant(PERSON, PERSONAL_INSTALLATION, {5071: True})
    assert account_access(world, PERSON, PERSONAL_INSTALLATION) is True


def test_the_personal_accounts_own_user_may_without_admin_rows(
    world: Session, grant: Callable[..., None]
) -> None:
    # A personal account's rule is the user's own ID; their repository flags do not matter.
    grant(PERSON, PERSONAL_INSTALLATION, {})
    assert account_access(world, PERSON, PERSONAL_INSTALLATION) is True


def test_another_user_listed_for_a_personal_installation_may_not(
    world: Session, grant: Callable[..., None]
) -> None:
    # A collaborator with admin on every repository of someone else's personal account.
    grant(OTHER_PERSON, PERSONAL_INSTALLATION, {5071: True})
    assert account_access(world, OTHER_PERSON, PERSONAL_INSTALLATION) is False


def test_an_organisation_user_with_admin_on_every_repository_may(
    world: Session, grant: Callable[..., None]
) -> None:
    grant(PERSON, ORG_INSTALLATION, EVERY_ORG_REPOSITORY)
    assert account_access(world, PERSON, ORG_INSTALLATION) is True


@pytest.mark.parametrize(
    "repositories",
    [
        pytest.param({5011: True, 5012: True}, id="admin on all but one"),
        pytest.param({5011: True, 5012: True, 5013: False}, id="push or less on one"),
        pytest.param({5011: False, 5012: False, 5013: False}, id="no admin at all"),
        pytest.param({}, id="no repository listed"),
    ],
)
def test_an_organisation_user_without_admin_on_every_repository_may_not(
    world: Session, grant: Callable[..., None], repositories: dict[int, bool]
) -> None:
    grant(PERSON, ORG_INSTALLATION, repositories)
    assert account_access(world, PERSON, ORG_INSTALLATION) is False


def test_a_never_synced_installation_gives_no_access(
    world: Session, grant: Callable[..., None]
) -> None:
    grant(PERSON, ORG_INSTALLATION, EVERY_ORG_REPOSITORY)
    world.get_one(Installation, ORG_INSTALLATION).repositories_synced_at = None
    world.flush()
    assert account_access(world, PERSON, ORG_INSTALLATION) is False


@pytest.mark.parametrize(
    ("age", "allowed"),
    [
        pytest.param(timedelta(minutes=90), True, id="synced within two runs"),
        pytest.param(timedelta(hours=2, minutes=1), False, id="sync older than two runs"),
    ],
)
def test_a_repository_list_not_synced_recently_gives_no_access(
    world: Session, grant: Callable[..., None], age: timedelta, allowed: bool
) -> None:
    # The reconciliation stopped or keeps failing, so a repository added since may be missing.
    grant(PERSON, ORG_INSTALLATION, EVERY_ORG_REPOSITORY)
    world.execute(
        update(Installation)
        .where(Installation.id == ORG_INSTALLATION)
        .values(repositories_synced_at=func.now() - age)
    )
    assert account_access(world, PERSON, ORG_INSTALLATION) is allowed


def test_an_installation_with_no_repository_gives_no_access(
    world: Session, grant: Callable[..., None]
) -> None:
    grant(PERSON, ORG_INSTALLATION, EVERY_ORG_REPOSITORY)
    retention.remove_repositories(world, ORG_INSTALLATION, [5011, 5012, 5013])
    assert account_access(world, PERSON, ORG_INSTALLATION) is False


def test_a_user_the_installation_is_not_listed_for_may_not(
    world: Session, grant: Callable[..., None]
) -> None:
    grant(PERSON, ORG_INSTALLATION, EVERY_ORG_REPOSITORY)
    world.query(UserInstallation).filter_by(user_id=PERSON).delete()
    world.flush()
    assert account_access(world, PERSON, ORG_INSTALLATION) is False
    assert account_access(world, PERSON, PERSONAL_INSTALLATION) is False


def test_a_foreign_or_unknown_installation_gives_no_access(
    world: Session, grant: Callable[..., None]
) -> None:
    grant(PERSON, ORG_INSTALLATION, EVERY_ORG_REPOSITORY)
    # Another organisation's installation, and one that does not exist.
    assert account_access(world, PERSON, OTHER_INSTALLATION) is False
    assert account_access(world, PERSON, 4999) is False


def test_admin_on_another_installations_repositories_does_not_count(
    world: Session, grant: Callable[..., None]
) -> None:
    grant(PERSON, ORG_INSTALLATION, {5011: True, 5012: True})
    grant(PERSON, OTHER_INSTALLATION, {5021: True})
    assert account_access(world, PERSON, ORG_INSTALLATION) is False
    assert account_access(world, PERSON, OTHER_INSTALLATION) is True


def test_a_repository_the_reconciliation_adds_removes_access_until_it_goes(
    world: Session, grant: Callable[..., None]
) -> None:
    grant(PERSON, ORG_INSTALLATION, EVERY_ORG_REPOSITORY)
    assert account_access(world, PERSON, ORG_INSTALLATION) is True
    # The reconciliation stores a repository the user has no admin on (its shared upsert)...
    store_repository(
        world, ORG_INSTALLATION, schemas.Repository(id=5014, name="new-app", private=True)
    )
    assert account_access(world, PERSON, ORG_INSTALLATION) is False
    # ...and removing it again (as the reconciliation does) gives access back.
    retention.remove_repositories(world, ORG_INSTALLATION, [5014])
    assert account_access(world, PERSON, ORG_INSTALLATION) is True


def test_one_user_s_admin_rows_never_count_for_another(
    world: Session, grant: Callable[..., None]
) -> None:
    grant(OTHER_PERSON, ORG_INSTALLATION, EVERY_ORG_REPOSITORY)
    grant(PERSON, ORG_INSTALLATION, {5011: False})
    assert account_access(world, OTHER_PERSON, ORG_INSTALLATION) is True
    assert account_access(world, PERSON, ORG_INSTALLATION) is False
