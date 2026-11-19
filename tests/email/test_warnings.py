"""The hourly step that queues run-out warnings: who gets one, which kind, and once only.

Invented accounts, people and jobs only, on the rolled-back test database. Each person has their
own account (a personal installation), so they see its whole use. NOW is 17 November 2026; every
cycle here runs from 1 November to 1 December, and Blacksmith's allowance is 3,000.
"""

import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.email import outbox, warnings
from burnbird.models import (
    Account,
    EmailOutbox,
    EmailPreference,
    GithubUser,
    Installation,
    Job,
    Repository,
    UserEmail,
    UserInstallation,
)

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
CYCLE = date(2026, 11, 1)
RESET = datetime(2026, 12, 1, tzinfo=UTC)
PERSON, OTHER = 7801, 7802
_JOB_IDS = itertools.count(1)


def _installation(user: int) -> int:
    return user + 1000


def _repository(user: int) -> int:
    return user * 10


@pytest.fixture
def people(session: Session) -> Session:
    """Two people, each with their own account, installed long ago, an address and warnings on."""
    for user in (PERSON, OTHER):
        session.add(GithubUser(id=user, login=f"example-{user}", last_sign_in_at=NOW))
        session.add(
            Account(id=user, login=f"example-{user}", type="User", github_plan="GitHub Free")
        )
        session.flush()
        session.add(
            Installation(
                id=_installation(user),
                account_id=user,
                created_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
        )
        session.flush()
        session.add_all(
            [
                UserInstallation(user_id=user, installation_id=_installation(user)),
                UserEmail(user_id=user, address=f"u{user}" + "@" + "example.invalid", read_at=NOW),
                EmailPreference(user_id=user, kind="usage_warnings", enabled=True, updated_at=NOW),
                Repository(
                    id=_repository(user),
                    installation_id=_installation(user),
                    name="app",
                    private=True,
                    backfilled_at=datetime(2026, 9, 2, tzinfo=UTC),
                ),
            ]
        )
    session.flush()
    return session


def _use(
    session: Session, user: int, minutes: dict[date, int], provider: str = "blacksmith"
) -> None:
    """Store one job a day; Blacksmith counts a minute as 1, GitHub's Linux runner $0.006."""
    runner, rate = (
        ("actions_linux", Decimal("0.006"))
        if provider == "github"
        else ("linux-x64-2vcpu", Decimal(1))
    )
    for day, amount in minutes.items():
        completed = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
        job_id = next(_JOB_IDS)
        session.add(
            Job(
                id=job_id,
                run_attempt=1,
                run_id=job_id,
                repository_id=_repository(user),
                labels=["x"],
                created_at=completed,
                started_at=completed - timedelta(minutes=amount),
                completed_at=completed,
                provider=provider,
                runner_class=runner,
                billed_minutes=Decimal(amount),
                amount=amount * rate,
            )
        )
    session.flush()


def _week(minutes: int) -> dict[date, int]:
    return {date(2026, 11, d): minutes for d in range(10, 17)}


# Blacksmith: 2,400 on 1 November is 80%, used-75, with no use in the last 7 days: no run-out.
USED_75 = {CYCLE: 2400}
# 2,700 is 90%.
USED_90 = {CYCLE: 2700}
# 2,400 and 60 a day from 10 to 16 November: 2,820 used (94%), 180 left at 60 a day, reached on
# 19 November: runs-out-before-reset and used-90 are both true.
RUNS_OUT = USED_75 | _week(60)
RAN_OUT = {CYCLE: 3000}


def _rows(session: Session, user: int = PERSON) -> dict[str, tuple[str, str | None]]:
    """Each run-out warning row of the user, by kind: its state and outcome."""
    session.expire_all()
    rows = session.scalars(select(EmailOutbox).where(EmailOutbox.user_id == user)).all()
    return {row.params["kind"]: (row.state, row.outcome) for row in rows}


def _record(
    session: Session,
    kind: str,
    state: str,
    outcome: str | None = None,
    *,
    cycle: date = CYCLE,
) -> None:
    """A warning of PERSON's Blacksmith allowance recorded earlier by a run or the sender."""
    dedupe_key = outbox.run_out_warning_key(kind, PERSON, "blacksmith", cycle, PERSON)
    assert outbox.enqueue(
        session,
        user_id=PERSON,
        account_id=PERSON,
        kind="usage_warnings",
        template="run_out_warning",
        dedupe_key=dedupe_key,
        params={
            "provider": "blacksmith",
            "kind": kind,
            "cycle_start": cycle.isoformat(),
            "repository_id": None,
            "runner_class": None,
        },
        now=NOW - timedelta(days=1),
        expires_at=RESET,
    )
    if state != "queued":
        session.execute(
            update(EmailOutbox)
            .where(EmailOutbox.dedupe_key == dedupe_key)
            .values(state=state, outcome=outcome, finished_at=NOW - timedelta(hours=2))
        )
    session.flush()


def _queue(sessions: sessionmaker[Session], user: int = PERSON, now: datetime = NOW) -> None:
    warnings.queue_account(sessions, user, now)


# ---------- what is queued ----------


def test_only_the_most_severe_new_warning_is_queued(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    _use(people, PERSON, RUNS_OUT)
    _queue(sessions)
    # used-90 is true too, but nothing is recorded for it: it may still go if runs-out does not.
    assert _rows(people) == {"runs-out-before-reset": ("queued", None)}
    people.expire_all()
    row = people.scalars(
        select(EmailOutbox).where(EmailOutbox.params["kind"].astext == "runs-out-before-reset")
    ).one()
    assert (row.kind, row.template, row.account_id, row.expires_at) == (
        "usage_warnings",
        "run_out_warning",
        PERSON,
        RESET,
    )
    assert row.dedupe_key == outbox.run_out_warning_key(
        "runs-out-before-reset", PERSON, "blacksmith", CYCLE, PERSON
    )
    # The cycle's top repository and runner size, as IDs only.
    assert row.params == {
        "provider": "blacksmith",
        "kind": "runs-out-before-reset",
        "cycle_start": "2026-11-01",
        "repository_id": _repository(PERSON),
        "runner_class": "linux-x64-2vcpu",
    }


@pytest.mark.parametrize("state", ["queued", "sent", "unknown"])
def test_a_less_severe_warning_never_follows_a_more_severe_one(
    people: Session, sessions: sessionmaker[Session], state: str
) -> None:
    # A run-out was queued, sent or maybe sent; now only 90% is new: it is not sent after it.
    _record(people, "runs-out-before-reset", state)
    _use(people, PERSON, USED_90)
    _queue(sessions)
    assert "used-90" not in _rows(people)


@pytest.mark.parametrize(
    "outcome",
    ["no_longer_true", "off", "suppressed", "stale_address", "expired", "superseded", "disabled"],
)
def test_a_skipped_more_severe_warning_does_not_block_a_less_severe_one(
    people: Session, sessions: sessionmaker[Session], outcome: str
) -> None:
    # The run-out was never sent (it stopped being true, the switch was off, and so on).
    _record(people, "runs-out-before-reset", "skipped", outcome)
    _use(people, PERSON, USED_90)
    _queue(sessions)
    assert _rows(people)["used-90"] == ("queued", None)


def test_a_queued_less_severe_warning_is_superseded(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    # 75% was queued but not sent yet (a cap, a retry); 90% is reached meanwhile.
    _record(people, "used-75", "queued")
    _use(people, PERSON, USED_90)
    _queue(sessions)
    rows = _rows(people)
    assert rows == {"used-75": ("skipped", "superseded"), "used-90": ("queued", None)}
    people.expire_all()
    superseded = people.scalars(
        select(EmailOutbox).where(EmailOutbox.params["kind"].astext == "used-75")
    ).one()
    assert (superseded.finished_at, superseded.retry_at) == (NOW, None)


# The run-out row was skipped at send, for good, and the forecast eased: 2,820 used (94%), so
# used-90 is still true.
def test_a_less_severe_warning_follows_a_skipped_more_severe_one(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    _use(people, PERSON, RUNS_OUT)
    _queue(sessions)
    people.execute(
        update(EmailOutbox)
        .where(EmailOutbox.params["kind"].astext == "runs-out-before-reset")
        .values(state="skipped", outcome="no_longer_true", finished_at=NOW)
    )
    _queue(sessions, now=NOW + timedelta(hours=1))
    assert _rows(people) == {
        "runs-out-before-reset": ("skipped", "no_longer_true"),
        "used-90": ("queued", None),
    }


# 2,220 on 1 November and 40 a day from 10 to 16 November: 2,500 used (83%), 500 left at 40 a day,
# reached on 28 November: runs-out-before-reset and used-75, not used-90.
RUNS_OUT_AT_83 = {CYCLE: 2220} | _week(40)


def test_a_superseded_queued_warning_comes_back_when_the_more_severe_one_is_skipped(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    _record(people, "used-75", "queued")
    _use(people, PERSON, RUNS_OUT_AT_83)
    _queue(sessions)
    assert _rows(people) == {
        "runs-out-before-reset": ("queued", None),
        "used-75": ("skipped", "superseded"),
    }
    # The run-out is skipped at send for good: used-75, still true, is queued again, the same row.
    people.execute(
        update(EmailOutbox)
        .where(EmailOutbox.params["kind"].astext == "runs-out-before-reset")
        .values(state="skipped", outcome="no_longer_true", finished_at=NOW)
    )
    later = NOW + timedelta(hours=1)
    _queue(sessions, now=later)
    assert _rows(people)["used-75"] == ("queued", None)
    people.expire_all()
    row = people.scalars(
        select(EmailOutbox).where(EmailOutbox.params["kind"].astext == "used-75")
    ).one()
    assert (row.finished_at, row.attempts, row.retry_at, row.expires_at) == (None, 0, later, RESET)
    assert people.scalar(select(func.count()).select_from(EmailOutbox)) == 2


def test_a_disabled_warning_comes_back_when_the_switch_is_on_again(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    # The switch was off when the sender reached it; the step runs again only once it is on.
    _record(people, "used-75", "skipped", "disabled")
    _use(people, PERSON, USED_75)
    _queue(sessions)
    assert _rows(people) == {"used-75": ("queued", None)}


def test_a_warning_skipped_while_suspended_comes_back_after(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    # Skipped at send while the installation was suspended. While it stays suspended, no run puts
    # the row back; once it is active again and 80% still holds, the next run does.
    _record(people, "used-75", "skipped", "suspended")
    _use(people, PERSON, USED_75)
    suspended = update(Installation).where(Installation.id == _installation(PERSON))
    people.execute(suspended.values(suspended=True))
    warnings.run(sessions, NOW)
    assert _rows(people) == {"used-75": ("skipped", "suspended")}
    people.execute(suspended.values(suspended=False))
    warnings.run(sessions, NOW + timedelta(hours=1))
    assert _rows(people) == {"used-75": ("queued", None)}


def test_a_warning_put_back_names_the_current_top_repository(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    # Recorded when no repository had used the allowance yet; now one has.
    _record(people, "used-75", "skipped", "disabled")
    _use(people, PERSON, USED_75)
    _queue(sessions)
    people.expire_all()
    row = people.scalars(select(EmailOutbox)).one()
    assert row.state == "queued"
    assert (row.params["repository_id"], row.params["runner_class"]) == (
        _repository(PERSON),
        "linux-x64-2vcpu",
    )


@pytest.mark.parametrize("state", ["sent", "unknown"])
def test_a_sent_or_unknown_warning_never_comes_back(
    people: Session, sessions: sessionmaker[Session], state: str
) -> None:
    _record(people, "used-75", state, "http:200")
    _use(people, PERSON, USED_75)
    _queue(sessions)
    assert _rows(people) == {"used-75": (state, "http:200")}


@pytest.mark.parametrize(
    "outcome", ["no_longer_true", "off", "suppressed", "stale_address", "expired"]
)
def test_a_final_skipped_warning_stays_final(
    people: Session, sessions: sessionmaker[Session], outcome: str
) -> None:
    _record(people, "used-75", "skipped", outcome)
    _use(people, PERSON, USED_75)
    _queue(sessions)
    assert _rows(people) == {"used-75": ("skipped", outcome)}


def test_a_kind_recorded_once_is_never_queued_again_in_its_cycle(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    _use(people, PERSON, USED_75)
    _queue(sessions)
    _queue(sessions, now=NOW + timedelta(hours=1))
    assert people.scalar(select(func.count()).select_from(EmailOutbox)) == 1


# ---------- which allowance, which kinds ----------


def test_github_queues_only_the_run_out_forecast(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    # GitHub Free, $12: 800 minutes ($4.80) and 150 a day for 7 days ($6.30) is $11.10, 92.5%;
    # $0.90 left at $0.90 a day is reached today. GitHub emails its own 90%, so only the run-out
    # is Burnbird's.
    _use(people, PERSON, {CYCLE: 800} | _week(150), provider="github")
    _queue(sessions)
    assert _rows(people) == {"runs-out-before-reset": ("queued", None)}


def test_each_provider_emails_only_its_listed_kinds(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    # 95% of GitHub's and 90% of Blacksmith's: only Blacksmith's percentage is emailed.
    _use(people, PERSON, {CYCLE: 1900}, provider="github")
    _use(people, PERSON, USED_90)
    _queue(sessions)
    people.expire_all()
    rows = people.scalars(select(EmailOutbox)).all()
    assert [(r.params["provider"], r.params["kind"], r.state) for r in rows] == [
        ("blacksmith", "used-90", "queued")
    ]


def test_each_allowance_gets_its_own_warning(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    # Both providers forecast a run-out: two allowances, two warnings.
    _use(people, PERSON, _week(190), provider="github")
    _use(people, PERSON, RUNS_OUT)
    _queue(sessions)
    people.expire_all()
    queued = people.scalars(select(EmailOutbox).where(EmailOutbox.state == "queued")).all()
    assert sorted(r.params["provider"] for r in queued) == ["blacksmith", "github"]


def test_a_new_cycle_warns_again(people: Session, sessions: sessionmaker[Session]) -> None:
    # The same warning was sent for October's cycle.
    _record(people, "used-75", "sent", "http:200", cycle=date(2026, 10, 1))
    _use(people, PERSON, USED_75)
    _queue(sessions)
    people.expire_all()
    november = people.scalars(
        select(EmailOutbox).where(EmailOutbox.params["cycle_start"].astext == "2026-11-01")
    ).one()
    assert november.state == "queued"


# ---------- who gets one ----------


def test_recipients_need_access(people: Session, sessions: sessionmaker[Session]) -> None:
    # OTHER is listed for PERSON's installation, but a personal account's whole use is its own
    # user's only.
    people.add(UserInstallation(user_id=OTHER, installation_id=_installation(PERSON)))
    _use(people, PERSON, USED_75)
    _queue(sessions)
    assert _rows(people, OTHER) == {}
    assert _rows(people) == {"used-75": ("queued", None)}


def test_a_stale_address_gets_none(people: Session, sessions: sessionmaker[Session]) -> None:
    _use(people, PERSON, USED_75)
    people.execute(
        update(UserEmail)
        .where(UserEmail.user_id == PERSON)
        .values(read_at=NOW - retention.EMAIL_FRESHNESS - timedelta(seconds=1))
    )
    _queue(sessions)
    assert _rows(people) == {}


def test_a_fresh_address_at_the_edge_gets_one(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    _use(people, PERSON, USED_75)
    people.execute(
        update(UserEmail)
        .where(UserEmail.user_id == PERSON)
        .values(read_at=NOW - retention.EMAIL_FRESHNESS)
    )
    _queue(sessions)
    assert _rows(people) == {"used-75": ("queued", None)}


def test_warnings_off_get_none(people: Session, sessions: sessionmaker[Session]) -> None:
    _use(people, PERSON, USED_75)
    people.execute(
        update(EmailPreference).where(EmailPreference.user_id == PERSON).values(enabled=False)
    )
    _queue(sessions)
    assert _rows(people) == {}


def test_a_suppressed_address_gets_none(people: Session, sessions: sessionmaker[Session]) -> None:
    _use(people, PERSON, USED_75)
    people.execute(
        update(UserEmail)
        .where(UserEmail.user_id == PERSON)
        .values(suppressed_at=NOW, suppressed_reason="hard_bounce")
    )
    _queue(sessions)
    assert _rows(people) == {}


def test_a_suspended_installation_gets_none(
    people: Session, sessions: sessionmaker[Session]
) -> None:
    _use(people, PERSON, USED_75)
    people.execute(
        update(Installation).where(Installation.id == _installation(PERSON)).values(suspended=True)
    )
    warnings.run(sessions, NOW)
    assert _rows(people) == {}


# ---------- the hourly run ----------


def test_the_task_claims_once_an_hour(people: Session, sessions: sessionmaker[Session]) -> None:
    _use(people, PERSON, USED_75)
    _use(people, OTHER, USED_75)
    assert warnings.run(sessions, NOW)
    assert not warnings.run(sessions, NOW + timedelta(minutes=59))
    assert warnings.run(sessions, NOW + timedelta(hours=1))
    assert _rows(people) == {"used-75": ("queued", None)}
    assert _rows(people, OTHER) == {"used-75": ("queued", None)}


def test_a_pass_stopped_halfway_and_run_again_queues_nothing_twice(
    people: Session,
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The first account fails; the pass logs it and goes on, and the next run queues it once.
    _use(people, PERSON, USED_75)
    _use(people, OTHER, USED_75)
    from burnbird.billing import usage

    state = usage.allowance_state

    def failing(session: Session, account_id: int, provider: str, now: datetime) -> object:
        if account_id == PERSON:
            raise RuntimeError("the database went away")
        return state(session, account_id, provider, now)

    monkeypatch.setattr(usage, "allowance_state", failing)
    assert warnings.run(sessions, NOW)
    assert _rows(people) == {}
    assert _rows(people, OTHER) == {"used-75": ("queued", None)}
    assert f"email warnings: account {PERSON} failed" in caplog.text
    monkeypatch.setattr(usage, "allowance_state", state)
    assert warnings.run(sessions, NOW + timedelta(hours=1))
    people.expire_all()
    assert people.scalar(select(func.count()).select_from(EmailOutbox)) == 2
    assert _rows(people) == {"used-75": ("queued", None)}
