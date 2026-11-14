import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx2 import Response
from sqlalchemy import Engine, delete, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from burnbird.billing.classify import classify
from burnbird.billing.providers import GITHUB, Kind, load_table
from burnbird.github.events import MAX_ATTEMPTS, claim, process_next, process_pending
from burnbird.models import (
    Account,
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

type Body = dict[str, Any]

# Invented accounts: two organisations and a person.
ORG, OTHER_ORG, PERSON = 3001, 3002, 3003
INSTALLATION = {ORG: 4001, OTHER_ORG: 4002, PERSON: 4003}
REPOSITORY = {ORG: 5001, OTHER_ORG: 5002, PERSON: 5003}


def _now() -> datetime:
    return datetime.now(UTC)


def _run(sessions: sessionmaker[Session], now: datetime | None = None) -> int:
    return process_pending(sessions, now or _now(), 100)


def _accepted(response: Response) -> None:
    assert response.status_code == 202


def _install(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    account: int = ORG,
    kind: str = "Organization",
    installation_id: int | None = None,
    login: str | None = None,
) -> None:
    body = installation(
        action="created",
        installation_id=installation_id or INSTALLATION[account],
        account_id=account,
        login=login or f"example-{account}",
        kind=kind,
    )
    _accepted(deliver("installation", body))


def _job_body(workflow_job: Callable[..., Body], account: int = ORG, **fields: Any) -> Body:
    fields.setdefault("job_id", 1)
    fields.setdefault("repository_id", REPOSITORY[account])
    return workflow_job(installation_id=INSTALLATION[account], **fields)


def _jobs(session: Session) -> list[Job]:
    session.expire_all()
    return list(session.scalars(select(Job).order_by(Job.id, Job.run_attempt)))


def _delivery(session: Session, delivery: str) -> WebhookDelivery:
    session.expire_all()
    stored = session.get(WebhookDelivery, uuid.UUID(delivery))
    assert stored is not None
    return stored


# ---------- job rows ----------


@pytest.mark.parametrize("private", [True, False])
def test_a_completed_job_becomes_a_billed_job_row(
    private: bool,
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation)
    _accepted(deliver("workflow_job", _job_body(workflow_job, private=private)))
    assert _run(sessions) == 2
    [job] = _jobs(session)
    standard = classify(["ubuntu-latest"], "GitHub Actions", private)
    assert standard.runner is not None
    assert standard.usd_per_minute is not None
    # 2 minutes 30 seconds, rounded up to whole minutes; standard runners are free when public.
    assert job.billed_minutes == Decimal(3)
    assert job.amount == Decimal(3) * standard.usd_per_minute
    assert (job.amount == 0) is not private
    assert (job.provider, job.runner_class) == (GITHUB, standard.runner.id)
    assert (job.run_id, job.workflow_name, job.labels) == (10, "ci", ["ubuntu-latest"])
    repository = session.get(Repository, REPOSITORY[ORG])
    assert repository is not None
    assert (repository.installation_id, repository.private) == (INSTALLATION[ORG], private)


def test_a_rerun_attempt_is_a_second_row(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation)
    for attempt in (1, 2):
        _accepted(deliver("workflow_job", _job_body(workflow_job, run_attempt=attempt)))
    _run(sessions)
    assert [(job.id, job.run_attempt) for job in _jobs(session)] == [(1, 1), (1, 2)]


def test_the_same_job_in_a_new_delivery_rewrites_its_row(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation)
    _accepted(deliver("workflow_job", _job_body(workflow_job, conclusion="failure")))
    _accepted(deliver("workflow_job", _job_body(workflow_job, conclusion="success")))
    _run(sessions)
    [job] = _jobs(session)
    assert job.conclusion == "success"


def test_a_job_no_runner_took_is_stored_unbilled(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation)
    body = _job_body(
        workflow_job, runner_id=None, started_at=None, conclusion="cancelled", workflow_name=None
    )
    _accepted(deliver("workflow_job", body))
    _run(sessions)
    [job] = _jobs(session)
    assert (job.billed_minutes, job.amount, job.workflow_name) == (None, None, None)


def test_a_larger_runner_is_sized_by_the_accounts_label_setting(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    larger = next(
        row
        for row in load_table().runners
        if row.provider == GITHUB and row.kind == Kind.LARGER and not row.labels
    )
    _install(deliver, installation)
    _run(sessions)
    session.add(LabelSize(account_id=ORG, label="example-large", size=larger.id))
    session.flush()
    body = _job_body(workflow_job, labels=["example-large"], runner_group_name="example-group")
    _accepted(deliver("workflow_job", body))
    _run(sessions)
    [job] = _jobs(session)
    assert job.runner_class == larger.id


# ---------- duplicates ----------


def test_a_duplicate_delivery_is_processed_once(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation)
    delivery = str(uuid.uuid4())
    _accepted(deliver("workflow_job", _job_body(workflow_job), delivery=delivery))
    _accepted(deliver("workflow_job", _job_body(workflow_job), delivery=delivery))
    assert _run(sessions) == 2
    processed_at = _delivery(session, delivery).processed_at
    assert processed_at is not None
    # A redelivery after processing is answered and not processed again.
    _accepted(deliver("workflow_job", _job_body(workflow_job), delivery=delivery))
    assert _run(sessions) == 0
    assert _delivery(session, delivery).processed_at == processed_at
    assert len(_jobs(session)) == 1


# ---------- failures ----------


def test_a_job_for_an_unknown_installation_waits_for_it(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    delivery = str(uuid.uuid4())
    _accepted(deliver("workflow_job", _job_body(workflow_job), delivery=delivery))
    now = _now()
    assert process_next(sessions, now)
    waiting = _delivery(session, delivery)
    assert (waiting.attempts, waiting.retry_at, waiting.processed_at) == (
        1,
        now + timedelta(minutes=2),
        None,
    )
    # The installation arrives next; the waiting job does not block it.
    _install(deliver, installation)
    assert _run(sessions, now) == 1
    assert _jobs(session) == []
    assert _run(sessions, now + timedelta(minutes=2)) == 1
    assert len(_jobs(session)) == 1


def test_a_delivery_that_keeps_failing_is_given_up(
    deliver: Callable[..., Response],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    delivery = str(uuid.uuid4())
    _accepted(deliver("workflow_job", _job_body(workflow_job), delivery=delivery))
    later = _now()
    for _ in range(MAX_ATTEMPTS):
        assert process_next(sessions, later)
        later += timedelta(days=1)
    assert not process_next(sessions, later + timedelta(days=30))
    stored = _delivery(session, delivery)
    assert (stored.attempts, stored.processed_at) == (MAX_ATTEMPTS, None)
    assert caplog.text.count(delivery) == MAX_ATTEMPTS
    assert "gave up" in caplog.records[-1].getMessage()


def test_a_failure_leaves_nothing_of_its_delivery(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation)
    _run(sessions)
    # Out of range for the attempt column: the job insert fails after the repository upsert.
    delivery = str(uuid.uuid4())
    body = _job_body(workflow_job, run_attempt=2**31)
    _accepted(deliver("workflow_job", body, delivery=delivery))
    assert _run(sessions) == 1
    assert _delivery(session, delivery).attempts == 1
    assert session.get(Repository, REPOSITORY[ORG]) is None
    assert _jobs(session) == []


# ---------- installations ----------


def test_a_new_account_starts_on_the_free_plan_of_its_type(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation, ORG, "Organization")
    _install(deliver, installation, PERSON, "User")
    _run(sessions)
    plans = {
        account: plan for account, plan in session.execute(select(Account.id, Account.github_plan))
    }
    assert plans == {ORG: "GitHub Free for organizations", PERSON: "GitHub Free"}
    stored = session.get(Installation, INSTALLATION[ORG])
    assert stored is not None
    assert (stored.account_id, stored.suspended) == (ORG, False)
    assert stored.created_at == datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def test_a_repeated_install_updates_the_login_and_keeps_the_chosen_plan(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation)
    _run(sessions)
    account = session.get(Account, ORG)
    assert account is not None
    account.github_plan = "GitHub Team"
    session.flush()
    _install(deliver, installation, login="example-renamed")
    _run(sessions)
    session.expire_all()
    account = session.get(Account, ORG)
    assert account is not None
    assert (account.login, account.github_plan) == ("example-renamed", "GitHub Team")


def test_an_install_replacing_a_stored_one_uninstalls_the_old_one(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation)
    _accepted(deliver("workflow_job", _job_body(workflow_job)))
    _run(sessions)
    _install(deliver, installation, installation_id=4009)
    _run(sessions)
    session.expire_all()
    assert session.get(Installation, INSTALLATION[ORG]) is None
    assert session.get(Installation, 4009) is not None
    assert _jobs(session) == []


@pytest.mark.parametrize("installation_id", [INSTALLATION[ORG], 9999])
def test_suspend_and_unsuspend_set_the_flag(
    installation_id: int,
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation)
    _run(sessions)
    flags = []
    for action in ("suspend", "unsuspend"):
        _accepted(
            deliver("installation", {"action": action, "installation": {"id": installation_id}})
        )
        # An installation that is not stored has nothing to change, and that is no failure.
        assert _run(sessions) == 1
        session.expire_all()
        stored = session.get(Installation, INSTALLATION[ORG])
        assert stored is not None
        flags.append(stored.suspended)
    assert flags == ([True, False] if installation_id == INSTALLATION[ORG] else [False, False])
    assert (
        session.scalar(select(WebhookDelivery.attempts).where(WebhookDelivery.attempts > 0))
        is None
    )


def test_an_uninstall_deletes_the_account_and_everything_under_it(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    for account in (ORG, OTHER_ORG):
        _install(deliver, installation, account)
        _accepted(deliver("workflow_job", _job_body(workflow_job, account, job_id=account)))
    _run(sessions)
    session.add(LabelSize(account_id=ORG, label="example-large", size="linux_4_core"))
    session.flush()
    _accepted(
        deliver("installation", installation(action="deleted", installation_id=INSTALLATION[ORG]))
    )
    assert _run(sessions) == 1
    session.expire_all()
    assert list(session.scalars(select(Account.id))) == [OTHER_ORG]
    assert list(session.scalars(select(Installation.id))) == [INSTALLATION[OTHER_ORG]]
    assert list(session.scalars(select(Repository.id))) == [REPOSITORY[OTHER_ORG]]
    assert [job.id for job in _jobs(session)] == [OTHER_ORG]
    assert list(session.scalars(select(LabelSize.account_id))) == []


def test_removed_repositories_go_with_their_jobs(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    repositories_removed: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    for account in (ORG, OTHER_ORG):
        _install(deliver, installation, account)
        _accepted(deliver("workflow_job", _job_body(workflow_job, account, job_id=account)))
    _accepted(deliver("workflow_job", _job_body(workflow_job, job_id=9, repository_id=5009)))
    _run(sessions)
    body = repositories_removed(
        installation_id=INSTALLATION[ORG], repository_ids=[REPOSITORY[ORG], REPOSITORY[OTHER_ORG]]
    )
    _accepted(deliver("installation_repositories", body))
    assert _run(sessions) == 1
    session.expire_all()
    assert sorted(session.scalars(select(Repository.id))) == [REPOSITORY[OTHER_ORG], 5009]
    assert [job.id for job in _jobs(session)] == [9, OTHER_ORG]


# ---------- revoked authorization ----------


def _revoked(user: int) -> Body:
    return {
        "action": "revoked",
        "sender": {"login": f"example-{user}", "id": user, "node_id": "N_example", "type": "User"},
    }


def test_a_revoked_authorization_signs_the_user_out_and_keeps_the_jobs(
    deliver: Callable[..., Response],
    installation: Callable[..., Body],
    workflow_job: Callable[..., Body],
    sessions: sessionmaker[Session],
    session: Session,
) -> None:
    _install(deliver, installation, PERSON, kind="User")
    _accepted(deliver("workflow_job", _job_body(workflow_job, PERSON)))
    _run(sessions)
    now = _now()
    for user, n in ((PERSON, 1), (ORG, 2)):
        session.add(GithubUser(id=user, login=f"example-{user}", last_sign_in_at=now))
        session.flush()
        session.add_all(
            [
                UserSession(
                    id_sha256=bytes([n]) * 32,
                    user_id=user,
                    created_at=now,
                    expires_at=now + timedelta(hours=8),
                ),
                UserInstallation(user_id=user, installation_id=INSTALLATION[PERSON]),
                UserRepository(user_id=user, repository_id=REPOSITORY[PERSON]),
            ]
        )
    session.flush()
    _accepted(deliver("github_app_authorization", _revoked(PERSON)))
    assert _run(sessions) == 1
    session.expire_all()
    # Only the revoking user goes; revoking is not uninstalling, so the jobs stay.
    assert list(session.scalars(select(GithubUser.id))) == [ORG]
    assert list(session.scalars(select(UserSession.user_id))) == [ORG]
    assert list(session.scalars(select(UserInstallation.user_id))) == [ORG]
    assert list(session.scalars(select(UserRepository.user_id))) == [ORG]
    assert len(_jobs(session)) == 1


def test_a_revoked_authorization_of_an_unknown_user_is_processed(
    deliver: Callable[..., Response], sessions: sessionmaker[Session], session: Session
) -> None:
    delivery = str(uuid.uuid4())
    _accepted(deliver("github_app_authorization", _revoked(9999), delivery=delivery))
    assert _run(sessions) == 1
    assert _delivery(session, delivery).processed_at is not None


# ---------- two workers ----------


def test_a_claimed_delivery_is_skipped_by_another_worker(engine: Engine) -> None:
    # Two connections need committed rows; they are deleted again at the end.
    delivery = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(WebhookDelivery).values(
                id=delivery,
                received_at=_now(),
                event="installation",
                action="suspend",
                payload={"installation": {"id": 1}},
            )
        )
    try:
        with Session(engine) as first, Session(engine) as second:
            claimed = claim(first, _now())
            assert claimed is not None
            assert claimed.id == delivery
            # Without SKIP LOCKED this would wait for the lock and fail after a second.
            second.execute(text("SET LOCAL lock_timeout = '1s'"))
            assert claim(second, _now()) is None
    finally:
        with engine.begin() as connection:
            connection.execute(delete(WebhookDelivery).where(WebhookDelivery.id == delivery))
