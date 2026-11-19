"""Each allowance's use per UTC day, read from stored jobs, on the rolled-back test database.

Invented accounts, repositories and jobs only. Each job's amount comes from the real classifier and
counting, so a public repository's GitHub minutes are free here as in production.
"""

import itertools
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from burnbird.billing.classify import classify
from burnbird.billing.counting import Job as BillableJob
from burnbird.billing.counting import bill
from burnbird.billing.forecast import forecast
from burnbird.billing.usage import Usage, allowance_state, complete_from, daily_totals, top_usage
from burnbird.models import Account, Installation, Job, Repository

ORG, OTHER_ORG = 3001, 3002
INSTALLATION, OTHER_INSTALLATION = 4001, 4002
PRIVATE, PUBLIC, OTHER_PRIVATE, ELSEWHERE = 5011, 5012, 5013, 5021
# Installed long before November, every repository backfilled: every day here is complete.
INSTALLED = datetime(2026, 9, 3, 8, tzinfo=UTC)
NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
# One label per runner size; GitHub's runs in the GitHub-hosted runner group.
LABELS = {
    "github": ("ubuntu-latest", "GitHub Actions"),
    "blacksmith": ("blacksmith-2vcpu-ubuntu-2404", "Default"),
    "blacksmith-4": ("blacksmith-4vcpu-ubuntu-2404", "Default"),
    "self-hosted": ("self-hosted", "Default"),
    "unknown": ("my-own-runner", "Default"),
}

_ids = itertools.count(9_000_001)


def nov(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


@pytest.fixture
def world(session: Session) -> Session:
    """Two organisations, each installed: three repositories under one, one under the other."""
    for account_id, login in ((ORG, "example-org"), (OTHER_ORG, "example-other-org")):
        session.add(
            Account(
                id=account_id,
                login=login,
                type="Organization",
                github_plan="GitHub Free for organizations",
            )
        )
    session.flush()
    for installation_id, account_id in ((INSTALLATION, ORG), (OTHER_INSTALLATION, OTHER_ORG)):
        session.add(Installation(id=installation_id, account_id=account_id, created_at=INSTALLED))
    session.flush()
    for repository_id, installation_id, private in (
        (PRIVATE, INSTALLATION, True),
        (PUBLIC, INSTALLATION, False),
        (OTHER_PRIVATE, INSTALLATION, True),
        (ELSEWHERE, OTHER_INSTALLATION, True),
    ):
        session.add(
            Repository(
                id=repository_id,
                installation_id=installation_id,
                name="app",
                private=private,
                backfilled_at=INSTALLED,
            )
        )
    session.flush()
    return session


@pytest.fixture
def add_job(world: Session) -> Callable[..., Decimal | None]:
    """Store one job of ``minutes`` whole minutes completed at ``completed``; return its amount."""

    def add(
        repository_id: int, completed: datetime, minutes: int, runner: str = "github"
    ) -> Decimal | None:
        label, group = LABELS[runner]
        private = repository_id != PUBLIC
        classification = classify((label,), group, private)
        started = completed - timedelta(minutes=minutes)
        billed = bill(BillableJob(7, started, completed), classification)
        assert billed is not None
        job_id = next(_ids)
        world.add(
            Job(
                id=job_id,
                run_attempt=1,
                run_id=job_id * 10,
                repository_id=repository_id,
                labels=[label],
                runner_group_name=group,
                created_at=started,
                started_at=started,
                completed_at=completed,
                provider=classification.provider,
                runner_class=None if classification.runner is None else classification.runner.id,
                billed_minutes=billed.minutes,
                amount=billed.amount,
            )
        )
        world.flush()
        return billed.amount

    return add


def test_daily_totals_count_each_job_on_its_utc_day_inside_the_window(
    world: Session, add_job: Callable[..., Decimal | None]
) -> None:
    add_job(PRIVATE, datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=UTC), 1)  # before
    add_job(PRIVATE, datetime(2026, 11, 1, tzinfo=UTC), 2)  # the first instant
    # 23:30 at -05:00 is 04:30 UTC on 2 November.
    add_job(PRIVATE, datetime(2026, 11, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5))), 3)
    add_job(PRIVATE, datetime(2026, 11, 2, 23, 59, 59, 999999, tzinfo=UTC), 4)  # the last instant
    add_job(PRIVATE, datetime(2026, 11, 3, tzinfo=UTC), 5)  # after
    add_job(OTHER_PRIVATE, nov(1), 10, "blacksmith")
    totals = daily_totals(
        world, INSTALLATION, ("github", "blacksmith"), date(2026, 11, 1), date(2026, 11, 3)
    )
    minute = Decimal("0.006")
    assert totals == {
        "github": {date(2026, 11, 1): 2 * minute, date(2026, 11, 2): 7 * minute},
        "blacksmith": {date(2026, 11, 1): Decimal(10)},
    }


def test_daily_totals_count_only_the_installation_s_jobs_of_the_providers_asked(
    world: Session, add_job: Callable[..., Decimal | None]
) -> None:
    add_job(PRIVATE, nov(1), 2)
    add_job(PRIVATE, nov(1), 3, "blacksmith")
    add_job(PRIVATE, nov(1), 50, "self-hosted")
    add_job(PRIVATE, nov(1), 60, "unknown")  # no amount
    add_job(ELSEWHERE, nov(1), 70)  # another installation's
    totals = daily_totals(world, INSTALLATION, ("github",), date(2026, 11, 1), date(2026, 11, 2))
    assert totals == {"github": {date(2026, 11, 1): 2 * Decimal("0.006")}}
    assert daily_totals(world, INSTALLATION, (), date(2026, 11, 1), date(2026, 11, 2)) == {}


def test_a_public_repository_s_github_minutes_use_none_of_the_allowance(
    world: Session, add_job: Callable[..., Decimal | None]
) -> None:
    assert add_job(PUBLIC, nov(2), 500) == 0
    add_job(PRIVATE, nov(2), 100)
    state = allowance_state(world, ORG, "github", NOW)
    assert state is not None
    assert state.forecast.used == Decimal("0.600")
    # A repository that used none of the allowance is never the top one.
    start = date(2026, 11, 1)
    assert top_usage(world, INSTALLATION, "github", start, NOW.date()) == (
        Usage(PRIVATE, "actions_linux", Decimal("0.600")),
    )


@pytest.mark.parametrize(
    ("plan", "amount"),
    [
        ("GitHub Free for organizations", 12),
        ("GitHub Team", 18),
        ("GitHub Enterprise Cloud", 300),
    ],
)
def test_the_github_allowance_is_the_account_s_plan(
    world: Session, plan: str, amount: int
) -> None:
    world.execute(update(Account).where(Account.id == ORG).values(github_plan=plan))
    state = allowance_state(world, ORG, "github", NOW)
    assert state is not None
    assert (state.allowance.plan, state.allowance.amount) == (plan, Decimal(amount))
    assert state.forecast.left == amount


def test_a_plan_the_table_does_not_list_is_refused(world: Session) -> None:
    world.execute(update(Account).where(Account.id == ORG).values(github_plan="GitHub Galaxy"))
    with pytest.raises(ValueError, match="account 3001: GitHub plan 'GitHub Galaxy'"):
        allowance_state(world, ORG, "github", NOW)


def test_each_allowance_cycles_on_its_own_day(
    world: Session, add_job: Callable[..., Decimal | None]
) -> None:
    world.execute(
        update(Account)
        .where(Account.id == ORG)
        .values(github_cycle_day=1, blacksmith_cycle_day=15)
    )
    for day in (10, 16):
        add_job(PRIVATE, nov(day), 100)
        add_job(PRIVATE, nov(day), 100, "blacksmith")
    github = allowance_state(world, ORG, "github", NOW)
    blacksmith = allowance_state(world, ORG, "blacksmith", NOW)
    assert github is not None
    assert blacksmith is not None
    assert (github.forecast.start, github.forecast.used) == (date(2026, 11, 1), Decimal("1.200"))
    assert (blacksmith.forecast.start, blacksmith.forecast.reset) == (
        date(2026, 11, 15),
        date(2026, 12, 15),
    )
    assert (blacksmith.allowance.amount, blacksmith.forecast.used) == (Decimal(3000), Decimal(100))


def test_complete_from_is_the_day_after_the_install_until_every_repository_is_backfilled(
    world: Session,
) -> None:
    world.execute(
        update(Installation)
        .where(Installation.id == INSTALLATION)
        .values(created_at=datetime(2026, 11, 10, 13, tzinfo=UTC))
    )
    world.execute(update(Account).where(Account.id == ORG).values(blacksmith_cycle_day=15))
    world.execute(update(Repository).where(Repository.id == PUBLIC).values(backfilled_at=None))
    today = date(2026, 11, 12)
    assert complete_from(world, INSTALLATION, today) == date(2026, 11, 11)
    world.execute(update(Repository).where(Repository.id == PUBLIC).values(backfilled_at=NOW))
    # The earlier of the two cycle starts: Blacksmith's, 15 October.
    assert complete_from(world, INSTALLATION, today) == date(2026, 10, 15)
    # Another installation's repository not backfilled changes nothing.
    world.execute(update(Repository).where(Repository.id == ELSEWHERE).values(backfilled_at=None))
    assert complete_from(world, INSTALLATION, today) == date(2026, 10, 15)


def test_complete_from_is_never_before_the_day_after_the_data_was_deleted(world: Session) -> None:
    world.execute(
        update(Account)
        .where(Account.id == ORG)
        .values(data_deleted_at=datetime(2026, 11, 6, 15, tzinfo=UTC))
    )
    assert complete_from(world, INSTALLATION, date(2026, 11, 12)) == date(2026, 11, 7)
    assert complete_from(world, OTHER_INSTALLATION, date(2026, 11, 12)) == date(2026, 9, 4)


def test_a_fresh_install_counts_its_first_partial_day_as_used_but_not_in_the_pace(
    world: Session, add_job: Callable[..., Decimal | None]
) -> None:
    world.execute(
        update(Installation)
        .where(Installation.id == INSTALLATION)
        .values(created_at=datetime(2026, 11, 10, 9, tzinfo=UTC))
    )
    world.execute(update(Repository).where(Repository.id == PUBLIC).values(backfilled_at=None))
    add_job(PRIVATE, nov(10), 1000, "blacksmith")  # the install day: only its later hours
    for day in range(11, 15):
        add_job(PRIVATE, nov(day), 50, "blacksmith")
    state = allowance_state(world, ORG, "blacksmith", nov(15))
    assert state is not None
    assert state.complete_from == date(2026, 11, 11)
    assert (state.forecast.used, state.forecast.pace, state.forecast.pace_days) == (
        Decimal(1200),
        Decimal(50),
        4,
    )
    assert state.forecast.cycle_pace == 50


def test_the_state_s_pace_reaches_back_past_the_cycle_start(
    world: Session, add_job: Callable[..., Decimal | None]
) -> None:
    for day in range(27, 32):
        add_job(PRIVATE, datetime(2026, 10, day, 12, tzinfo=UTC), 40, "blacksmith")
    add_job(PRIVATE, nov(1), 100, "blacksmith")
    add_job(PRIVATE, nov(2), 100, "blacksmith")
    state = allowance_state(world, ORG, "blacksmith", nov(3))
    assert state is not None
    assert (state.forecast.pace, state.forecast.pace_days, state.forecast.used) == (
        Decimal(50),
        7,
        Decimal(200),
    )


def test_the_page_and_the_email_share_the_forecast_rule(
    world: Session, add_job: Callable[..., Decimal | None]
) -> None:
    daily: dict[date, Decimal] = {}
    for day, minutes in zip(range(1, 20), itertools.cycle((90, 140, 220, 60)), strict=False):
        add_job(PRIVATE, nov(day), minutes, "blacksmith")
        daily[date(2026, 11, day)] = Decimal(minutes)
    state = allowance_state(world, ORG, "blacksmith", NOW)
    assert state is not None
    expected = forecast(daily, Decimal(3000), 1, NOW.date(), complete_from=date(2026, 9, 4))
    assert state.forecast == expected
    assert expected.runs_out is not None


def test_no_allowance_no_installation_or_an_unknown_provider_has_no_state(world: Session) -> None:
    assert allowance_state(world, ORG, "self-hosted", NOW) is None
    world.add(Account(id=3003, login="example-gone", type="User", github_plan="GitHub Free"))
    world.flush()
    assert allowance_state(world, 3003, "github", NOW) is None
    with pytest.raises(ValueError, match="provider 'gitlab'"):
        allowance_state(world, ORG, "gitlab", NOW)


def test_top_usage_ranks_each_repository_and_runner_size_inside_the_window(
    world: Session, add_job: Callable[..., Decimal | None]
) -> None:
    add_job(PRIVATE, nov(2), 10, "blacksmith")
    add_job(PRIVATE, nov(3), 15, "blacksmith")
    add_job(PRIVATE, nov(3), 10, "blacksmith-4")  # weight 2: 20 units
    add_job(OTHER_PRIVATE, nov(4), 30, "blacksmith")
    add_job(OTHER_PRIVATE, nov(4), 99)  # GitHub's, another provider
    add_job(PRIVATE, datetime(2026, 10, 31, 23, 59, tzinfo=UTC), 500, "blacksmith")  # before
    add_job(PRIVATE, datetime(2026, 11, 5, tzinfo=UTC), 500, "blacksmith")  # after
    add_job(ELSEWHERE, nov(2), 900, "blacksmith")  # another installation's
    ranked = top_usage(world, INSTALLATION, "blacksmith", date(2026, 11, 1), date(2026, 11, 5))
    assert ranked == (
        Usage(OTHER_PRIVATE, "linux-x64-2vcpu", Decimal(30)),
        Usage(PRIVATE, "linux-x64-2vcpu", Decimal(25)),
        Usage(PRIVATE, "linux-x64-4vcpu", Decimal(20)),
    )
    assert (
        top_usage(world, INSTALLATION, "self-hosted", date(2026, 11, 1), date(2026, 11, 5)) == ()
    )
