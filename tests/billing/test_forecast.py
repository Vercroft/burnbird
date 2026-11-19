from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from burnbird.billing.classify import classify
from burnbird.billing.counting import Job, bill, per_day
from burnbird.billing.forecast import (
    AllowanceWarning,
    Forecast,
    WarningKind,
    cycle,
    first_complete_day,
    forecast,
)

USED_75 = AllowanceWarning(WarningKind.USED_75, None)
USED_90 = AllowanceWarning(WarningKind.USED_90, None)


def nov(day: int) -> date:
    return date(2026, 11, day)


def october(day: int) -> date:
    return date(2026, 10, day)


# An installation backfilled long before November: every day of the tests' windows is complete.
LONG_AGO = date(2026, 9, 1)


def runs_out(on: date) -> AllowanceWarning:
    return AllowanceWarning(WarningKind.RUNS_OUT_BEFORE_RESET, on)


def ran_out(on: date) -> AllowanceWarning:
    return AllowanceWarning(WarningKind.RAN_OUT, on)


def units(series: Mapping[date, int | str]) -> dict[date, Decimal]:
    return {day: Decimal(value) for day, value in series.items()}


@pytest.mark.parametrize(
    ("today", "day", "start", "reset"),
    [
        (date(2026, 11, 13), 1, date(2026, 11, 1), date(2026, 12, 1)),
        (date(2026, 11, 1), 1, date(2026, 11, 1), date(2026, 12, 1)),
        (date(2026, 11, 13), 15, date(2026, 10, 15), date(2026, 11, 15)),
        (date(2027, 1, 30), 31, date(2026, 12, 31), date(2027, 1, 31)),
        (date(2027, 1, 31), 31, date(2027, 1, 31), date(2027, 2, 28)),
        (date(2027, 2, 27), 31, date(2027, 1, 31), date(2027, 2, 28)),
        (date(2027, 2, 28), 31, date(2027, 2, 28), date(2027, 3, 31)),
        (date(2027, 3, 30), 31, date(2027, 2, 28), date(2027, 3, 31)),
        (date(2027, 3, 31), 31, date(2027, 3, 31), date(2027, 4, 30)),
        (date(2028, 2, 29), 30, date(2028, 2, 29), date(2028, 3, 30)),
        (date(2028, 2, 28), 30, date(2028, 1, 30), date(2028, 2, 29)),
        (date(2026, 12, 31), 31, date(2026, 12, 31), date(2027, 1, 31)),
    ],
)
def test_the_cycle_comes_from_the_day_of_the_month_clamped_in_short_months(
    today: date, day: int, start: date, reset: date
) -> None:
    assert cycle(today, day) == (start, reset)


@pytest.mark.parametrize("day", [0, 32, -1])
def test_a_cycle_day_outside_1_to_31_is_refused(day: int) -> None:
    with pytest.raises(ValueError, match="cycle day"):
        cycle(nov(13), day)


@pytest.mark.parametrize("allowance", ["0", "-1"])
def test_an_allowance_that_is_not_positive_is_refused(allowance: str) -> None:
    with pytest.raises(ValueError, match="allowance"):
        forecast({}, Decimal(allowance), 1, nov(13), complete_from=LONG_AGO)


def test_use_before_the_reset_belongs_to_the_previous_cycle() -> None:
    daily = units({nov(30): 500, date(2026, 12, 1): 3})
    assert forecast(daily, Decimal(3000), 1, nov(30), complete_from=LONG_AGO).used == 500
    december = forecast(daily, Decimal(3000), 1, date(2026, 12, 1), complete_from=LONG_AGO)
    assert (december.start, december.used) == (date(2026, 12, 1), Decimal(3))


def test_use_before_a_clamped_reset_belongs_to_the_previous_cycle() -> None:
    daily = units({date(2027, 2, 27): 9, date(2027, 2, 28): 1})
    assert forecast(daily, Decimal(3000), 31, date(2027, 2, 28), complete_from=LONG_AGO).used == 1


def test_without_a_full_day_of_complete_data_there_is_no_pace() -> None:
    result = forecast(units({nov(1): 50}), Decimal(3000), 1, nov(1), complete_from=nov(1))
    assert result == Forecast(
        start=nov(1),
        reset=date(2026, 12, 1),
        used=Decimal(50),
        left=Decimal(2950),
        pace=None,
        pace_days=0,
        cycle_pace=None,
        runs_out=None,
        runs_out_by_cycle_pace=None,
        warnings=(),
    )


def test_the_percentage_warnings_apply_on_the_first_day() -> None:
    result = forecast(units({nov(1): 95}), Decimal(100), 1, nov(1), complete_from=nov(1))
    assert result.warnings == (USED_90,)


def test_today_is_left_out_of_the_pace() -> None:
    daily = units({nov(d): 10 for d in range(1, 5)} | {nov(5): 99})
    result = forecast(daily, Decimal(3000), 1, nov(5), complete_from=nov(1))
    assert (result.used, result.pace, result.pace_days, result.cycle_pace) == (
        Decimal(139),
        Decimal(10),
        4,
        Decimal(10),
    )


def test_seven_full_days_make_the_window() -> None:
    daily = units({nov(d): 10 for d in range(1, 8)} | {nov(8): 500})
    result = forecast(daily, Decimal(30000), 1, nov(8), complete_from=nov(1))
    assert (result.pace, result.pace_days, result.cycle_pace) == (Decimal(10), 7, Decimal(10))


def test_the_pace_is_the_last_seven_full_days_and_the_cycle_pace_every_full_day() -> None:
    daily = units({nov(1): 1000, nov(2): 1000} | {nov(d): 7 for d in range(3, 10)})
    result = forecast(daily, Decimal(30000), 1, nov(10), complete_from=nov(1))
    assert result.pace == 7
    assert result.cycle_pace == Decimal(2049) / Decimal(9)


def test_a_pace_of_zero_lasts_the_cycle() -> None:
    result = forecast(units({nov(1): 100}), Decimal(3000), 1, nov(10), complete_from=nov(1))
    assert result.pace == 0
    assert (result.runs_out, result.runs_out_by_cycle_pace, result.warnings) == (None, None, ())


def test_a_tiny_pace_lasts_the_cycle_without_overflowing() -> None:
    daily = units({october(d): "1E-20" for d in range(26, 32)} | {nov(1): "1E-20"})
    result = forecast(daily, Decimal(3000), 1, nov(2), complete_from=LONG_AGO)
    assert result.pace == Decimal("1E-20")
    assert (result.runs_out, result.runs_out_by_cycle_pace) == (None, None)


def test_a_run_out_on_the_reset_day_lasts_the_cycle() -> None:
    # 700 left at 100 a day is 7 days from 2026-11-24: 2026-12-01, the reset.
    daily = units({nov(d): 100 for d in range(17, 24)})
    result = forecast(daily, Decimal(1400), 1, nov(24), complete_from=LONG_AGO)
    assert (result.left, result.pace, result.runs_out) == (Decimal(700), Decimal(100), None)
    assert result.warnings == ()


def test_a_run_out_the_day_before_the_reset_warns() -> None:
    # 699 left at 100 a day is 6.99 days, rounded down to 6: 2026-11-30.
    daily = units({nov(d): 100 for d in range(17, 24)} | {nov(24): 1})
    result = forecast(daily, Decimal(1400), 1, nov(24), complete_from=LONG_AGO)
    assert result.runs_out == nov(30)
    assert result.warnings == (runs_out(nov(30)),)


def test_a_run_out_within_a_day_is_today() -> None:
    # The pace is 10: October's six days at 10, and 1 November's 50, the busiest, left out.
    daily = units({october(d): 10 for d in range(26, 32)} | {nov(1): 50, nov(2): 45})
    result = forecast(daily, Decimal(100), 1, nov(2), complete_from=LONG_AGO)
    assert (result.pace, result.left) == (Decimal(10), Decimal(5))
    assert result.runs_out == nov(2)
    assert result.warnings == (USED_90, runs_out(nov(2)))


def test_a_used_up_allowance_ran_out_on_the_day_it_was_crossed() -> None:
    daily = units({nov(1): 40, nov(2): 70, nov(3): 10})
    result = forecast(daily, Decimal(100), 1, nov(4), complete_from=LONG_AGO)
    assert result.left == -20
    assert (result.runs_out, result.runs_out_by_cycle_pace) == (nov(2), nov(2))
    assert result.warnings == (USED_90, ran_out(nov(2)))


def test_exactly_the_allowance_is_used_up() -> None:
    result = forecast(
        units({nov(1): 40, nov(2): 60}), Decimal(100), 1, nov(3), complete_from=nov(1)
    )
    assert result.left == 0
    assert result.warnings == (USED_90, ran_out(nov(2)))


def test_an_allowance_used_up_today_ran_out_today() -> None:
    result = forecast(
        units({nov(1): 50, nov(3): 60}), Decimal(100), 1, nov(3), complete_from=nov(1)
    )
    assert result.warnings == (USED_90, ran_out(nov(3)))


@pytest.mark.parametrize(
    ("used", "warnings"),
    [("74.99", ()), ("75", (USED_75,)), ("89.99", (USED_75,)), ("90", (USED_90,))],
)
def test_only_the_highest_percentage_warning_is_given(
    used: str, warnings: tuple[AllowanceWarning, ...]
) -> None:
    result = forecast(units({nov(1): used}), Decimal(100), 1, nov(1), complete_from=nov(1))
    assert result.warnings == warnings


def test_the_real_case_of_2026_11_10() -> None:
    # Blacksmith, 1,588 of 3,000 free minutes used on 2026-11-10, cycle from 2026-11-01. The note
    # gives only those totals, so this daily series is constructed: it sums to 1,588, busier in the
    # last full week.
    daily = units(
        {
            nov(1): 20,
            nov(2): 100,
            nov(3): 180,
            nov(4): 190,
            nov(5): 210,
            nov(6): 220,
            nov(7): 200,
            nov(8): 190,
            nov(9): 210,
            nov(10): 68,  # today, a partial day: counted in used, never in a pace
        }
    )
    result = forecast(daily, Decimal(3000), 1, nov(10), complete_from=nov(1))
    assert (result.start, result.reset) == (nov(1), date(2026, 12, 1))
    assert (result.used, result.left) == (Decimal(1588), Decimal(1412))
    # pace: 2026-11-03..09 without the busiest day (220 on 2026-11-06), 1,180 / 6 = 196.67 a day;
    # 1,412 / 196.67 = 7.18, rounded down to 7 days.
    assert (result.pace, result.pace_days) == (Decimal(1180) / Decimal(6), 7)
    assert result.runs_out == nov(17)
    # cycle pace: 1,520 over 2026-11-01..09 / 9 = 168.89 a day; 1,412 / 168.89 = 8.36, rounded
    # down to 8.
    assert result.cycle_pace == Decimal(1520) / Decimal(9)
    assert result.runs_out_by_cycle_pace == nov(18)
    # 52.9% used: no percentage warning. Blacksmith keeps running, so for it this is the day
    # billing starts.
    assert result.warnings == (runs_out(nov(17)),)
    # The note's "around 14 November" came from Blacksmith's own dashboard. A 7-day mean cannot
    # reach it from these totals: even all 1,588 in the last 7 full days is a pace of 226.9, and
    # 1,412 / 226.9 = 6.2 gives 2026-11-16.


# Constructed: 1 November 2026 is a Sunday; a weekday uses 70, a weekend day 14.
def n1_series(today: date) -> dict[date, Decimal]:
    use = {nov(1): 600} | {nov(d): 70 if nov(d).weekday() < 5 else 14 for d in range(2, 31)}
    return units({day: value for day, value in use.items() if day <= today})


def test_one_busy_first_day_forecasts_no_run_out() -> None:
    # The plan's series N1 on a fresh install: 600 on the first day, then a steady 70 a weekday.
    # It ends the cycle at 2,182 of 3,000, so a run-out warning on any day would be false.
    assert sum(n1_series(nov(30)).values()) == 2182
    for day in range(2, 31):
        result = forecast(n1_series(nov(day)), Decimal(3000), 1, nov(day), complete_from=nov(1))
        assert result.runs_out is None, nov(day)
        assert result.warnings == (), nov(day)


def test_the_pace_reaches_back_past_the_cycle_start() -> None:
    # The last 7 full days are 27 October to 2 November: five at 40 in the previous cycle, two at
    # 100 in this one. The busiest, one of the 100s, is left out: (5 x 40 + 100) / 6 = 50.
    daily = units({october(d): 40 for d in range(27, 32)} | {nov(1): 100, nov(2): 100, nov(3): 5})
    result = forecast(daily, Decimal(3000), 1, nov(3), complete_from=LONG_AGO)
    assert (result.pace, result.pace_days) == (Decimal(50), 7)
    # The previous cycle's use is never counted as used, nor in the cycle pace.
    assert (result.used, result.cycle_pace) == (Decimal(205), Decimal(100))


def test_the_busiest_day_is_left_out_of_the_pace() -> None:
    spike = units({nov(d): 10 for d in range(1, 8)} | {nov(4): 500})
    assert forecast(spike, Decimal(3000), 1, nov(8), complete_from=nov(1)).pace == 10
    # Only one day is left out, even when two share the top.
    two = spike | units({nov(5): 500})
    assert forecast(two, Decimal(3000), 1, nov(8), complete_from=nov(1)).pace == Decimal(550) / 6


def test_a_fresh_install_needs_four_full_days() -> None:
    # Complete from 10 November, at 10 a day of an allowance of 100.
    daily = units({nov(d): 10 for d in range(10, 15)})
    three = forecast(daily, Decimal(100), 1, nov(13), complete_from=nov(10))
    assert (three.pace, three.pace_days, three.runs_out, three.warnings) == (None, 3, None, ())
    four = forecast(daily, Decimal(100), 1, nov(14), complete_from=nov(10))
    # 50 left at 10 a day: 5 days, 19 November.
    assert (four.pace, four.pace_days) == (Decimal(10), 4)
    assert four.warnings == (runs_out(nov(19)),)


def test_no_pace_counts_days_before_complete_from() -> None:
    # 1 to 4 November are not complete (the backfill has not finished): they count as used but
    # never in a pace.
    daily = units({nov(d): 500 for d in range(1, 5)} | {nov(d): 10 for d in range(5, 11)})
    result = forecast(daily, Decimal(30000), 1, nov(10), complete_from=nov(5))
    assert (result.pace, result.pace_days, result.cycle_pace) == (Decimal(10), 5, Decimal(10))
    assert result.used == 2060


def test_no_pace_counts_days_before_the_data_was_deleted() -> None:
    # Delete my data on 6 November at 15:00 deleted every job before it, so 4 to 6 November hold
    # nothing, and 6 November only its last hours: none of them is a day of no use.
    deleted = datetime(2026, 11, 6, 15, tzinfo=UTC)
    complete = first_complete_day(datetime(2026, 9, 1, tzinfo=UTC), True, nov(1), deleted)
    assert complete == nov(7)
    daily = units({nov(d): 100 for d in range(7, 12)})
    result = forecast(daily, Decimal(3000), 1, nov(11), complete_from=complete)
    assert (result.pace, result.pace_days) == (Decimal(100), 4)


@pytest.mark.parametrize(
    ("installed_at", "backfilled", "deleted_at", "complete"),
    [
        # Installed during the day: webhooks bring every job from the next day on.
        (datetime(2026, 11, 10, 13, tzinfo=UTC), False, None, nov(11)),
        # Every repository backfilled: from the earlier cycle start.
        (datetime(2026, 11, 10, 13, tzinfo=UTC), True, None, nov(1)),
        # Installed before the cycle: every day since the install is complete.
        (datetime(2026, 9, 3, 8, tzinfo=UTC), True, None, date(2026, 9, 4)),
        (datetime(2026, 9, 3, 8, tzinfo=UTC), False, None, date(2026, 9, 4)),
        # Never before the day after Delete my data.
        (datetime(2026, 9, 3, 8, tzinfo=UTC), True, datetime(2026, 11, 6, 15, tzinfo=UTC), nov(7)),
        (datetime(2026, 11, 10, 13, tzinfo=UTC), True, datetime(2026, 11, 6, tzinfo=UTC), nov(7)),
        # A time in another zone is read as its UTC day: 23:30 at -05:00 is 04:30 the next day.
        (
            datetime(2026, 11, 10, 23, 30, tzinfo=timezone(timedelta(hours=-5))),
            False,
            None,
            nov(12),
        ),
    ],
)
def test_the_data_is_complete_from_the_day_after_the_install_or_the_backfilled_cycle_start(
    installed_at: datetime, backfilled: bool, deleted_at: datetime | None, complete: date
) -> None:
    assert first_complete_day(installed_at, backfilled, nov(1), deleted_at) == complete


def test_jobs_flow_from_classification_to_a_forecast() -> None:
    def ended(completed: datetime, seconds: int) -> Job:
        return Job(1, completed - timedelta(seconds=seconds), completed)

    jobs = [
        ("blacksmith-4vcpu-ubuntu-2404", ended(datetime(2026, 11, 3, 12, tzinfo=UTC), 61)),
        ("blacksmith-2vcpu-ubuntu-2404", ended(datetime(2026, 11, 4, 12, tzinfo=UTC), 30)),
        ("blacksmith-2vcpu-ubuntu-2404", ended(datetime(2026, 11, 5, 8, tzinfo=UTC), 600)),
    ]
    billed = [bill(job, classify((label,), "Default", private=True)) for label, job in jobs]
    daily = per_day(b for b in billed if b is not None)
    assert daily == {nov(3): 4, nov(4): 1, nov(5): 10}
    result = forecast(daily, Decimal(3000), 1, nov(5), complete_from=nov(1))
    # Full days 2026-11-01..04 hold 0, 0, 4 and 1; without the busiest, 1 / 3 a day, far from
    # 2,985 left.
    assert (result.used, result.pace, result.runs_out) == (Decimal(15), Decimal(1) / 3, None)
