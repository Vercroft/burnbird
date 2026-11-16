import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Connection, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.auth.service import SignIn, store_sign_in
from burnbird.github import backfill
from burnbird.models import (
    Account,
    Base,
    EmailConsent,
    EmailPreference,
    GithubUser,
    Installation,
    Job,
    LabelSize,
    Repository,
    UserEmail,
    UserInstallation,
    UserRepository,
    UserSession,
    WebhookDelivery,
)
from burnbird.retention import (
    Deleted,
    Purged,
    delete_my_data,
    purge_deliveries,
    purge_jobs,
    purge_sessions,
    purge_stale_emails,
    remove_repositories,
    revoke_authorization,
    run_purges,
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


def _email(user: int, read_at: datetime) -> UserEmail:
    # Invented, on the reserved example.invalid domain.
    return UserEmail(
        user_id=user, address=f"user-{user}" + "@" + "example.invalid", read_at=read_at
    )


def _choices(user: int) -> list[EmailPreference | EmailConsent]:
    """One user's three email choices and a consent row for each."""
    return [
        *(
            EmailPreference(user_id=user, kind=kind, enabled=False, updated_at=RECENT)
            for kind in ("usage_warnings", "weekly_summary", "news")
        ),
        *(
            EmailConsent(
                user_id=user,
                kind=kind,
                enabled=False,
                source="default",
                text_version="0" * 12,
                at=RECENT,
            )
            for kind in ("usage_warnings", "weekly_summary", "news")
        ),
    ]


def _email_rows(session: Session, user: int) -> tuple[int, int]:
    session.expire_all()
    return (
        len(session.scalars(select(EmailPreference).where(EmailPreference.user_id == user)).all()),
        len(session.scalars(select(EmailConsent).where(EmailConsent.user_id == user)).all()),
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


def test_an_address_is_kept_exactly_30_days_after_a_sign_in_read_it(world: Session) -> None:
    world.add_all(
        [
            _email(PERSON, NOW - timedelta(days=30)),
            _email(MEMBER, NOW - timedelta(days=30) - TICK),
        ]
    )
    before = _rows(world)
    assert purge_stale_emails(world, NOW) == 1
    assert _deleted(before, _rows(world)) == {"user_email": {(MEMBER,)}}


def test_run_purges_runs_every_purge_and_logs_one_line_of_counts(
    world: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # One row just past each limit and one just inside it.
    world.add_all(
        [
            _job(10, ORG, NOW - timedelta(days=90)),
            _job(11, ORG, NOW - timedelta(days=90) - TICK),
            _delivery(uuid.UUID(int=10), NOW - timedelta(days=7)),
            _delivery(uuid.UUID(int=11), NOW - timedelta(days=7) - TICK),
            _session(MEMBER, NOW, 10),
            _session(MEMBER, NOW + TICK, 11),
            _email(PERSON, NOW - timedelta(days=30)),
            _email(MEMBER, NOW - timedelta(days=30) - TICK),
        ]
    )
    before = _rows(world)
    with caplog.at_level(logging.INFO, logger=retention.__name__):
        assert run_purges(world, NOW) == Purged(jobs=1, deliveries=1, sessions=1, emails=1)
    assert _deleted(before, _rows(world)) == {
        "job": {(11, 1)},
        "webhook_delivery": {(uuid.UUID(int=11),)},
        "session": {(bytes([10]) * 32,)},
        "user_email": {(MEMBER,)},
    }
    [record] = [r for r in caplog.records if r.name == retention.__name__]
    assert record.getMessage() == "purge: 1 jobs, 1 deliveries, 1 sessions, 1 email addresses"


@pytest.mark.parametrize(
    "purge", [purge_jobs, purge_deliveries, purge_sessions, purge_stale_emails]
)
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


def test_delete_my_data_removes_the_user_s_email_address(world: Session) -> None:
    world.add_all([_email(PERSON, RECENT), _email(MEMBER, RECENT)])
    before = _rows(world)
    assert delete_my_data(world, PERSON).user_emails == 1
    assert _deleted(before, _rows(world))["user_email"] == {(PERSON,)}


@pytest.mark.parametrize(
    "remove", [delete_my_data, revoke_authorization], ids=["delete my data", "revoke"]
)
def test_the_user_s_email_choices_and_consent_rows_go_with_them(
    world: Session, remove: Callable[[Session, int], Deleted]
) -> None:
    world.add_all([*_choices(PERSON), *_choices(MEMBER)])
    world.flush()
    deleted = remove(world, PERSON)
    assert (deleted.email_preferences, deleted.email_consents) == (3, 3)
    assert _email_rows(world, PERSON) == (0, 0)
    assert _email_rows(world, MEMBER) == (3, 3)


def test_delete_my_data_marks_when_the_personal_account_s_data_was_deleted(
    world: Session,
) -> None:
    before = world.scalar(select(func.now()))
    assert isinstance(before, datetime)
    delete_my_data(world, PERSON)
    world.expire_all()
    person = world.get_one(Account, PERSON)
    assert person.data_deleted_at is not None
    assert person.data_deleted_at >= before - timedelta(seconds=1)
    # An organisation's account is never marked by a user's request.
    assert world.get_one(Account, ORG).data_deleted_at is None


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


def test_a_revoked_user_loses_their_email_address(world: Session) -> None:
    world.add_all([_email(PERSON, RECENT), _email(MEMBER, RECENT)])
    before = _rows(world)
    assert revoke_authorization(world, PERSON).user_emails == 1
    assert _deleted(before, _rows(world))["user_email"] == {(PERSON,)}


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


# Locking several repositories: always in ID order.


@pytest.fixture
def sent(connection: Connection) -> Iterator[list[str]]:
    """The SQL of every statement sent on the test's connection, as compiled."""
    statements: list[str] = []

    def record(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(connection, "before_cursor_execute", record)
    yield statements
    event.remove(connection, "before_cursor_execute", record)


def _locks(statements: list[str]) -> list[str]:
    # The repository locks only: Delete my data also locks the user row.
    return [s for s in statements if s.startswith("SELECT repository.id") and " FOR " in s]


@pytest.mark.parametrize("lock", ["UPDATE", "NO KEY UPDATE", "KEY SHARE"])
def test_lock_repositories_locks_in_id_order(
    session: Session, sent: list[str], lock: retention.RowLock
) -> None:
    retention.lock_repositories(session, [5002, 5001], lock=lock)
    [statement] = _locks(sent)
    assert statement.rstrip().endswith(f"ORDER BY repository.id FOR {lock}")


@pytest.mark.parametrize(
    ("lock_several", "mode"),
    [
        pytest.param(
            lambda s: retention.delete_my_data(s, 7001), "FOR UPDATE", id="delete_my_data"
        ),
        pytest.param(lambda s: backfill.requeue(s, [5002, 5001]), "FOR KEY SHARE", id="requeue"),
        pytest.param(
            lambda s: store_sign_in(
                sessionmaker(bind=s.connection(), join_transaction_mode="create_savepoint"),
                SignIn(7001, "example-7001", (), (), 0),
                NOW,
                None,
            ),
            "FOR NO KEY UPDATE",
            id="store_sign_in",
        ),
    ],
)
def test_every_path_that_locks_several_repositories_locks_them_in_id_order(
    session: Session, sent: list[str], lock_several: Callable[[Session], object], mode: str
) -> None:
    lock_several(session)
    [lock] = _locks(sent)
    assert lock.rstrip().endswith(f"ORDER BY repository.id {mode}")
