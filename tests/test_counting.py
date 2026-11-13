from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from burnbird.classify import Classification, classify
from burnbird.counting import Billed, Job, bill, billed_minutes, per_day
from burnbird.providers import Rounding

# Invented jobs. Standard GitHub-hosted jobs report this runner group.
HOSTED = "GitHub Actions"
START = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
RUNNER = 7

BLACKSMITH_4VCPU = classify(("blacksmith-4vcpu-ubuntu-2404",), "Default", private=True)
LINUX_PRIVATE = classify(("ubuntu-latest",), HOSTED, private=True)
LINUX_PUBLIC = classify(("ubuntu-latest",), HOSTED, private=False)
SELF_HOSTED = classify(("self-hosted", "linux"), "Default", private=True)
UNKNOWN = classify(("my-own-runner",), "Default", private=True)


def job(seconds: float, start: datetime = START) -> Job:
    return Job(RUNNER, start, start + timedelta(seconds=seconds))


def billed(seconds: float, classification: Classification) -> Billed:
    result = bill(job(seconds), classification)
    assert result is not None
    return result


@pytest.mark.parametrize(
    ("seconds", "minutes"), [(0, 0), (1, 1), (59, 1), (60, 1), (61, 2), (89, 2), (120, 2)]
)
def test_each_job_is_rounded_up_to_a_whole_minute(seconds: int, minutes: int) -> None:
    assert billed_minutes(
        START, START + timedelta(seconds=seconds), Rounding.JOB_UP_TO_MINUTE
    ) == Decimal(minutes)


def test_a_fraction_of_a_second_past_a_minute_rounds_up() -> None:
    end = START + timedelta(seconds=60, microseconds=1)
    assert billed_minutes(START, end, Rounding.JOB_UP_TO_MINUTE) == 2


def test_many_short_jobs_each_round_up() -> None:
    jobs = [billed(5, LINUX_PRIVATE) for _ in range(10)]
    assert sum(b.minutes for b in jobs) == 10


def test_no_rounding_counts_exact_seconds() -> None:
    exact = Decimal(61) / Decimal(60)
    assert billed_minutes(START, START + timedelta(seconds=61), Rounding.NONE) == exact
    assert billed_minutes(START, START + timedelta(seconds=90), Rounding.NONE) == Decimal("1.5")


def test_every_run_attempt_is_billed() -> None:
    # Attempt 1 and its re-run, attempt 2: two job records of the same run.
    first = bill(job(61), LINUX_PRIVATE)
    rerun = bill(job(61, START + timedelta(minutes=10)), LINUX_PRIVATE)
    assert first is not None
    assert rerun is not None
    assert per_day([first, rerun]) == {date(2026, 11, 10): Decimal("0.024")}


def test_queue_time_is_never_billed() -> None:
    # Created (queued) at 08:30, started at 09:00, completed at 09:02: 2 minutes billed, not 32.
    # Job has no created time, so queue time cannot enter the bill.
    assert billed(120, LINUX_PRIVATE).minutes == 2


@pytest.mark.parametrize(
    "unbilled",
    [
        Job(None, START, START + timedelta(minutes=1)),
        Job(RUNNER, None, START + timedelta(minutes=1)),
        Job(RUNNER, START, None),
    ],
    ids=["no runner", "never started", "not completed"],
)
def test_a_job_without_a_runner_or_a_timestamp_is_not_billed(unbilled: Job) -> None:
    assert bill(unbilled, LINUX_PRIVATE) is None


def test_a_naive_timestamp_is_refused() -> None:
    naive = datetime(2026, 11, 10, 9, 0)
    with pytest.raises(ValueError, match="timezone"):
        bill(Job(RUNNER, naive, naive + timedelta(minutes=1)), LINUX_PRIVATE)


def test_a_completion_before_the_start_counts_as_zero() -> None:
    assert billed(-30, LINUX_PRIVATE).minutes == 0


def test_a_standard_runner_is_free_on_a_public_repository_but_its_minutes_count() -> None:
    public = billed(61, LINUX_PUBLIC)
    private = billed(61, LINUX_PRIVATE)
    assert public.minutes == private.minutes == 2
    assert public.amount == 0
    assert private.amount == Decimal("0.012")


def test_a_blacksmith_job_is_weighted() -> None:
    assert billed(61, BLACKSMITH_4VCPU) == Billed(Decimal(2), Decimal(4), date(2026, 11, 10))


def test_a_self_hosted_job_counts_exact_time_at_no_cost() -> None:
    assert billed(90, SELF_HOSTED) == Billed(Decimal("1.5"), Decimal(0), date(2026, 11, 10))


def test_an_unknown_job_counts_exact_time_with_no_amount() -> None:
    assert billed(90, UNKNOWN) == Billed(Decimal("1.5"), None, date(2026, 11, 10))


def test_per_day_skips_jobs_without_an_amount() -> None:
    assert per_day([billed(90, UNKNOWN), billed(61, BLACKSMITH_4VCPU)]) == {
        date(2026, 11, 10): Decimal(4)
    }


@pytest.mark.parametrize(
    ("start", "end", "day"),
    [
        (
            datetime(2026, 11, 30, 23, 58, tzinfo=UTC),
            datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC),
            date(2026, 11, 30),
        ),
        (
            datetime(2026, 12, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 12, 1, 0, 0, 1, tzinfo=UTC),
            date(2026, 12, 1),
        ),
        (
            datetime(2026, 11, 30, 23, 50, tzinfo=UTC),
            datetime(2026, 12, 1, 0, 10, tzinfo=UTC),
            date(2026, 12, 1),
        ),
    ],
    ids=["just before the reset", "just after the reset", "across the reset"],
)
def test_a_job_counts_whole_on_the_utc_day_it_completed(
    start: datetime, end: datetime, day: date
) -> None:
    result = bill(Job(RUNNER, start, end), LINUX_PRIVATE)
    assert result is not None
    assert result.day == day


def test_the_completion_day_is_taken_in_utc() -> None:
    # 2026-12-01 01:00 in UTC+2 is 2026-11-30 23:00 UTC.
    plus_two = datetime(2026, 12, 1, 1, 0, tzinfo=timezone(timedelta(hours=2)))
    result = bill(Job(RUNNER, plus_two - timedelta(minutes=5), plus_two), LINUX_PRIVATE)
    assert result is not None
    assert result.day == date(2026, 11, 30)
