from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from burnbird.billing.classify import classify
from burnbird.billing.counting import Job, bill, per_day
from burnbird.billing.forecast import AllowanceWarning, Forecast, WarningKind, cycle, forecast

USED_75 = AllowanceWarning(WarningKind.USED_75, None)
USED_90 = AllowanceWarning(WarningKind.USED_90, None)


def nov(day: int) -> date:
    return date(2026, 11, day)


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
        forecast({}, Decimal(allowance), 1, nov(13))


def test_use_before_the_reset_belongs_to_the_previous_cycle() -> None:
    daily = units({nov(30): 500, date(2026, 12, 1): 3})
    assert forecast(daily, Decimal(3000), 1, nov(30)).used == 500
    december = forecast(daily, Decimal(3000), 1, date(2026, 12, 1))
    assert (december.start, december.used) == (date(2026, 12, 1), Decimal(3))


def test_use_before_a_clamped_reset_belongs_to_the_previous_cycle() -> None:
    daily = units({date(2027, 2, 27): 9, date(2027, 2, 28): 1})
    assert forecast(daily, Decimal(3000), 31, date(2027, 2, 28)).used == 1


def test_on_the_first_day_of_a_cycle_there_is_no_pace() -> None:
    result = forecast(units({nov(1): 50}), Decimal(3000), 1, nov(1))
    assert result == Forecast(
        start=nov(1),
        reset=date(2026, 12, 1),
        used=Decimal(50),
        left=Decimal(2950),
        pace=None,
        cycle_pace=None,
        runs_out=None,
        runs_out_by_cycle_pace=None,
        warnings=(),
    )


def test_the_percentage_warnings_apply_on_the_first_day() -> None:
    assert forecast(units({nov(1): 95}), Decimal(100), 1, nov(1)).warnings == (USED_90,)


def test_one_full_day_gives_a_pace_and_today_is_left_out() -> None:
    result = forecast(units({nov(1): 10, nov(2): 99}), Decimal(3000), 1, nov(2))
    assert (result.used, result.pace, result.cycle_pace) == (
        Decimal(109),
        Decimal(10),
        Decimal(10),
    )


def test_seven_full_days_make_the_window() -> None:
    daily = units({nov(d): 10 for d in range(1, 8)} | {nov(8): 500})
    result = forecast(daily, Decimal(30000), 1, nov(8))
    assert (result.pace, result.cycle_pace) == (Decimal(10), Decimal(10))


def test_the_pace_is_the_last_seven_full_days_and_the_cycle_pace_every_full_day() -> None:
    daily = units({nov(1): 1000, nov(2): 1000} | {nov(d): 7 for d in range(3, 10)})
    result = forecast(daily, Decimal(30000), 1, nov(10))
    assert result.pace == 7
    assert result.cycle_pace == Decimal(2049) / Decimal(9)


def test_a_pace_of_zero_lasts_the_cycle() -> None:
    result = forecast(units({nov(1): 100}), Decimal(3000), 1, nov(10))
    assert result.pace == 0
    assert (result.runs_out, result.runs_out_by_cycle_pace, result.warnings) == (None, None, ())


def test_a_tiny_pace_lasts_the_cycle_without_overflowing() -> None:
    result = forecast(units({nov(1): "1E-20"}), Decimal(3000), 1, nov(2))
    assert (result.runs_out, result.runs_out_by_cycle_pace) == (None, None)


def test_a_run_out_on_the_reset_day_lasts_the_cycle() -> None:
    # 700 left at 100 a day is 7 days from 2026-11-24: 2026-12-01, the reset.
    result = forecast(units({nov(d): 100 for d in range(17, 24)}), Decimal(1400), 1, nov(24))
    assert (result.left, result.pace, result.runs_out) == (Decimal(700), Decimal(100), None)
    assert result.warnings == ()


def test_a_run_out_the_day_before_the_reset_warns() -> None:
    # 699 left at 100 a day is 6.99 days, rounded down to 6: 2026-11-30.
    daily = units({nov(d): 100 for d in range(17, 24)} | {nov(24): 1})
    result = forecast(daily, Decimal(1400), 1, nov(24))
    assert result.runs_out == nov(30)
    assert result.warnings == (runs_out(nov(30)),)


def test_a_run_out_within_a_day_is_today() -> None:
    result = forecast(units({nov(1): 50, nov(2): 40}), Decimal(100), 1, nov(2))
    assert result.runs_out == nov(2)
    assert result.warnings == (USED_90, runs_out(nov(2)))


def test_a_used_up_allowance_ran_out_on_the_day_it_was_crossed() -> None:
    daily = units({nov(1): 40, nov(2): 70, nov(3): 10})
    result = forecast(daily, Decimal(100), 1, nov(4))
    assert result.left == -20
    assert (result.runs_out, result.runs_out_by_cycle_pace) == (nov(2), nov(2))
    assert result.warnings == (USED_90, ran_out(nov(2)))


def test_exactly_the_allowance_is_used_up() -> None:
    result = forecast(units({nov(1): 40, nov(2): 60}), Decimal(100), 1, nov(3))
    assert result.left == 0
    assert result.warnings == (USED_90, ran_out(nov(2)))


def test_an_allowance_used_up_today_ran_out_today() -> None:
    result = forecast(units({nov(1): 50, nov(3): 60}), Decimal(100), 1, nov(3))
    assert result.warnings == (USED_90, ran_out(nov(3)))


@pytest.mark.parametrize(
    ("used", "warnings"),
    [("74.99", ()), ("75", (USED_75,)), ("89.99", (USED_75,)), ("90", (USED_90,))],
)
def test_only_the_highest_percentage_warning_is_given(
    used: str, warnings: tuple[AllowanceWarning, ...]
) -> None:
    assert forecast(units({nov(1): used}), Decimal(100), 1, nov(1)).warnings == warnings


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
    result = forecast(daily, Decimal(3000), 1, nov(10))
    assert (result.start, result.reset) == (nov(1), date(2026, 12, 1))
    assert (result.used, result.left) == (Decimal(1588), Decimal(1412))
    # pace: 1,400 over 2026-11-03..09 / 7 = 200 a day; 1,412 / 200 = 7.06, rounded down to 7 days.
    assert result.pace == 200
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
    result = forecast(daily, Decimal(3000), 1, nov(5))
    # Full days 2026-11-01..04 hold 5 units: 1.25 a day, far from 2,985 left.
    assert (result.used, result.pace, result.runs_out) == (Decimal(15), Decimal("1.25"), None)
