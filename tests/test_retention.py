import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from burnbird.models import (
    Account,
    Base,
    GithubUser,
    Installation,
    Job,
    LabelSize,
    Repository,
    UserInstallation,
    UserRepository,
    UserSession,
    WebhookDelivery,
)
from burnbird.retention import (
    Deleted,
    delete_my_data,
    purge_deliveries,
    purge_jobs,
    purge_sessions,
    remove_repositories,
    revoke_authorization,
    uninstall,
)

NOW = datetime(2026, 11, 13, 3, 0, tzinfo=UTC)
TICK = timedelta(microseconds=1)
RECENT = NOW - timedelta(days=1)

# Invented accounts: a person with a personal installation, and two organisations.
PERSON, ORG, OTHER_ORG, MEMBER = 101, 102, 103, 104
INSTALLATION = {PERSON: 1001, ORG: 1002, OTHER_ORG: 1003}
REPOSITORY = {PERSON: 5001, ORG: 5002, OTHER_ORG: 5003}

type Rows = dict[str, set[tuple[object, ...]]]


def _rows(session: Session) -> Rows:
    """Every stored row of every table, by primary key."""
    session.flush()
    rows: Rows = {}
    for table in Base.metadata.sorted_tables:
        key = table.primary_key.columns
        rows[table.name] = {tuple(row) for row in session.execute(select(*key))}
    return rows


def _job(job_id: int, account: int, created_at: datetime = RECENT) -> Job:
    return Job(
        id=job_id,
        run_attempt=1,
        run_id=job_id * 10,
        repository_id=REPOSITORY[account],
        workflow_name="ci",
        labels=["ubuntu-latest"],
        runner_group_name="GitHub Actions",
        runner_name="GitHub Actions 1",
        created_at=created_at,
        started_at=created_at,
        completed_at=created_at + timedelta(minutes=2),
        conclusion="success",
        provider="github",
        runner_class="actions_linux",
        billed_minutes=Decimal(2),
        amount=Decimal("0.012"),
    )


def _delivery(
    delivery_id: uuid.UUID, received_at: datetime, processed_at: datetime | None = None
) -> WebhookDelivery:
    return WebhookDelivery(
        id=delivery_id,
        received_at=received_at,
        processed_at=processed_at,
        event="installation",
        action="suspend",
        payload={"installation": {"id": INSTALLATION[ORG]}},
    )


def _session(user: int, expires_at: datetime, n: int) -> UserSession:
    return UserSession(
        id_sha256=bytes([n]) * 32,
        user_id=user,
        created_at=expires_at - timedelta(hours=8),
        expires_at=expires_at,
    )


@pytest.fixture
def world(session: Session) -> Session:
    """Three installed accounts with jobs, two signed-in users, sessions and deliveries."""
    for account, login, kind in (
        (PERSON, "demo-user", "User"),
        (ORG, "demo-org", "Organization"),
        (OTHER_ORG, "other-demo-org", "Organization"),
    ):
        session.add(Account(id=account, login=login, type=kind, github_plan="GitHub Free"))
        session.flush()
        session.add(Installation(id=INSTALLATION[account], account_id=account, created_at=RECENT))
        session.flush()
        session.add(
            Repository(
                id=REPOSITORY[account],
                installation_id=INSTALLATION[account],
                name="app",
                private=True,
            )
        )
        session.add(LabelSize(account_id=account, label="big-runner", size="linux_4_core"))
    session.add_all(
        [
            GithubUser(id=PERSON, login="demo-user", last_sign_in_at=RECENT),
            GithubUser(id=MEMBER, login="demo-member", last_sign_in_at=RECENT),
        ]
    )
    session.flush()
    session.add_all(
        [
            *(
                _job(n, account)
                for n, account in ((1, PERSON), (2, PERSON), (3, ORG), (4, OTHER_ORG))
            ),
            UserInstallation(user_id=PERSON, installation_id=INSTALLATION[PERSON]),
            UserInstallation(user_id=PERSON, installation_id=INSTALLATION[ORG]),
            UserInstallation(user_id=MEMBER, installation_id=INSTALLATION[ORG]),
            UserInstallation(user_id=MEMBER, installation_id=INSTALLATION[OTHER_ORG]),
            UserRepository(user_id=PERSON, repository_id=REPOSITORY[PERSON]),
            UserRepository(user_id=PERSON, repository_id=REPOSITORY[ORG]),
            UserRepository(user_id=MEMBER, repository_id=REPOSITORY[ORG]),
            _session(PERSON, NOW + timedelta(hours=1), 1),
            _session(PERSON, NOW + timedelta(hours=2), 2),
            _session(MEMBER, NOW + timedelta(hours=3), 3),
            _delivery(uuid.UUID(int=1), RECENT, RECENT),
        ]
    )
    session.flush()
    return session


def _deleted(before: Rows, after: Rows) -> Rows:
    assert all(after[table] <= before[table] for table in before), "a row appeared"
    return {
        table: before[table] - after[table] for table in before if before[table] - after[table]
    }


# ---------- retention ----------


def test_jobs_are_kept_exactly_90_days(world: Session) -> None:
    world.add_all(
        [_job(10, ORG, NOW - timedelta(days=90)), _job(11, ORG, NOW - timedelta(days=90) - TICK)]
    )
    before = _rows(world)
    assert purge_jobs(world, NOW) == 1
    assert _deleted(before, _rows(world)) == {"job": {(11, 1)}}


def test_deliveries_are_kept_exactly_7_days(world: Session) -> None:
    kept, gone = uuid.UUID(int=10), uuid.UUID(int=11)
    world.add_all(
        [
            _delivery(kept, NOW - timedelta(days=7)),
            # Never processed: it goes all the same.
            _delivery(gone, NOW - timedelta(days=7) - TICK),
        ]
    )
    before = _rows(world)
    assert purge_deliveries(world, NOW) == 1
    assert _deleted(before, _rows(world)) == {"webhook_delivery": {(gone,)}}


def test_a_session_is_deleted_when_it_expires(world: Session) -> None:
    world.add_all([_session(MEMBER, NOW, 10), _session(MEMBER, NOW + TICK, 11)])
    before = _rows(world)
    assert purge_sessions(world, NOW) == 1
    assert _deleted(before, _rows(world)) == {"session": {(bytes([10]) * 32,)}}


@pytest.mark.parametrize("purge", [purge_jobs, purge_deliveries, purge_sessions])
def test_a_time_without_a_timezone_is_refused(
    world: Session, purge: Callable[[Session, datetime], int]
) -> None:
    with pytest.raises(ValueError, match="timezone"):
        purge(world, NOW.replace(tzinfo=None))


# ---------- delete my data ----------


def test_delete_my_data_removes_the_user_and_their_personal_jobs_only(world: Session) -> None:
    before = _rows(world)
    deleted = delete_my_data(world, PERSON)
    assert deleted == Deleted(
        jobs=2, sessions=2, user_installations=2, user_repositories=2, users=1
    )
    assert _deleted(before, _rows(world)) == {
        "job": {(1, 1), (2, 1)},
        "session": {(bytes([1]) * 32,), (bytes([2]) * 32,)},
        "user_installation": {(PERSON, INSTALLATION[PERSON]), (PERSON, INSTALLATION[ORG])},
        "user_repository": {(PERSON, REPOSITORY[PERSON]), (PERSON, REPOSITORY[ORG])},
        "github_user": {(PERSON,)},
    }


def test_delete_my_data_leaves_organisation_jobs(world: Session) -> None:
    # Only a User account is personal, even with the user's own ID: a user's request never
    # deletes an organisation's data.
    world.add(Account(id=MEMBER, login="member-demo-org", type="Organization", github_plan="x"))
    world.flush()
    world.add(Installation(id=1004, account_id=MEMBER, created_at=RECENT))
    world.flush()
    world.add(Repository(id=5004, installation_id=1004, name="app", private=True))
    world.flush()
    job = _job(5, ORG)
    job.repository_id = 5004
    world.add(job)
    before = _rows(world)
    assert delete_my_data(world, MEMBER) == Deleted(
        sessions=1, user_installations=2, user_repositories=1, users=1
    )
    assert "job" not in _deleted(before, _rows(world))


# ---------- revoked authorization ----------


def test_a_revoked_user_loses_sessions_and_lists_but_no_jobs(world: Session) -> None:
    before = _rows(world)
    deleted = revoke_authorization(world, PERSON)
    assert deleted == Deleted(sessions=2, user_installations=2, user_repositories=2, users=1)
    assert _deleted(before, _rows(world)) == {
        "session": {(bytes([1]) * 32,), (bytes([2]) * 32,)},
        "user_installation": {(PERSON, INSTALLATION[PERSON]), (PERSON, INSTALLATION[ORG])},
        "user_repository": {(PERSON, REPOSITORY[PERSON]), (PERSON, REPOSITORY[ORG])},
        "github_user": {(PERSON,)},
    }


def test_revoking_an_unknown_user_deletes_nothing(world: Session) -> None:
    before = _rows(world)
    assert revoke_authorization(world, 9999) == Deleted()
    assert _rows(world) == before


# ---------- uninstall ----------


def test_uninstall_removes_the_account_and_everything_under_it(world: Session) -> None:
    before = _rows(world)
    deleted = uninstall(world, INSTALLATION[ORG])
    assert deleted == Deleted(
        jobs=1,
        user_repositories=2,
        repositories=1,
        label_sizes=1,
        user_installations=2,
        installations=1,
        accounts=1,
    )
    assert _deleted(before, _rows(world)) == {
        "account": {(ORG,)},
        "installation": {(INSTALLATION[ORG],)},
        "repository": {(REPOSITORY[ORG],)},
        "job": {(3, 1)},
        "label_size": {(ORG, "big-runner")},
        "user_installation": {(PERSON, INSTALLATION[ORG]), (MEMBER, INSTALLATION[ORG])},
        "user_repository": {(PERSON, REPOSITORY[ORG]), (MEMBER, REPOSITORY[ORG])},
    }


def test_uninstalling_an_unknown_installation_deletes_nothing(world: Session) -> None:
    before = _rows(world)
    assert uninstall(world, 9999) == Deleted()
    assert _rows(world) == before


# ---------- repositories removed from an installation ----------


def test_removing_repositories_deletes_them_and_their_jobs(world: Session) -> None:
    before = _rows(world)
    deleted = remove_repositories(world, INSTALLATION[ORG], [REPOSITORY[ORG]])
    assert deleted == Deleted(jobs=1, user_repositories=2, repositories=1)
    assert _deleted(before, _rows(world)) == {
        "repository": {(REPOSITORY[ORG],)},
        "job": {(3, 1)},
        "user_repository": {(PERSON, REPOSITORY[ORG]), (MEMBER, REPOSITORY[ORG])},
    }


def test_removing_another_installations_repository_deletes_nothing(world: Session) -> None:
    before = _rows(world)
    assert (
        remove_repositories(world, INSTALLATION[ORG], [REPOSITORY[OTHER_ORG], 9999]) == Deleted()
    )
    assert _rows(world) == before
