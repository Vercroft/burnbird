"""How much of an allowance is used, how fast, and the day it runs out.

Pure functions, no I/O. Days are UTC dates; ``today`` is the caller's UTC date and counts as a
partial day: in what is used, never in a pace. Warnings are data; the pages word them.
"""

import calendar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import accumulate

PACE_DAYS = 7


class WarningKind(StrEnum):
    """Why an allowance needs attention."""

    USED_75 = "used-75"
    USED_90 = "used-90"
    RUNS_OUT_BEFORE_RESET = "runs-out-before-reset"
    RAN_OUT = "ran-out"


@dataclass(frozen=True, slots=True)
class AllowanceWarning:
    """One warning; ``on`` is the run-out date, None for the percentage kinds."""

    kind: WarningKind
    on: date | None


@dataclass(frozen=True, slots=True)
class Forecast:
    """One allowance in the current cycle.

    ``left`` is negative once the allowance is used up. ``pace`` is the mean daily use over the
    last 7 full days, or every full day when the cycle is younger; ``cycle_pace`` is the mean over
    every full day. Both are None on the cycle's first day. A run-out date is None when the
    allowance lasts the cycle or there is no pace yet; once used up it is the day it ran out.
    """

    start: date
    reset: date
    used: Decimal
    left: Decimal
    pace: Decimal | None
    cycle_pace: Decimal | None
    runs_out: date | None
    runs_out_by_cycle_pace: date | None
    warnings: tuple[AllowanceWarning, ...]


def cycle(today: date, day: int) -> tuple[date, date]:
    """Return the start of the cycle that holds ``today`` and the next reset.

    ``day`` is the day of the month the cycle starts on. In a month without it, the cycle starts on
    the month's last day, and later months go back to ``day``: Jan 31, Feb 28, Mar 31. This is
    Stripe's documented rule for such a day; neither GitHub nor Blacksmith states one:
    https://docs.stripe.com/billing/subscriptions/billing-cycle
    """
    if not 1 <= day <= 31:
        raise ValueError(f"cycle day {day} is not 1 to 31")
    offset = 0 if _anchor(today, 0, day) <= today else -1
    return _anchor(today, offset, day), _anchor(today, offset + 1, day)


def forecast(
    daily: Mapping[date, Decimal], allowance: Decimal, cycle_day: int, today: date
) -> Forecast:
    """Forecast one allowance from its use per UTC day."""
    if allowance <= 0:
        raise ValueError("allowance must be positive")
    start, reset = cycle(today, cycle_day)
    days = [start + timedelta(days=n) for n in range((today - start).days + 1)]
    use = [daily.get(day, Decimal(0)) for day in days]
    used = sum(use, Decimal(0))
    left = allowance - used
    full_days = use[:-1]
    pace = _mean(full_days[-PACE_DAYS:])
    cycle_pace = _mean(full_days)
    warnings: list[AllowanceWarning] = []
    runs_out: date | None
    by_cycle_pace: date | None
    if used * 100 >= allowance * 90:
        warnings.append(AllowanceWarning(WarningKind.USED_90, None))
    elif used * 100 >= allowance * 75:
        warnings.append(AllowanceWarning(WarningKind.USED_75, None))
    if left <= 0:
        ran_out = next(
            day for day, total in zip(days, accumulate(use), strict=True) if total >= allowance
        )
        warnings.append(AllowanceWarning(WarningKind.RAN_OUT, ran_out))
        runs_out = by_cycle_pace = ran_out
    else:
        runs_out = _runs_out(left, pace, today, reset)
        by_cycle_pace = _runs_out(left, cycle_pace, today, reset)
        if runs_out is not None:
            warnings.append(AllowanceWarning(WarningKind.RUNS_OUT_BEFORE_RESET, runs_out))
    return Forecast(
        start, reset, used, left, pace, cycle_pace, runs_out, by_cycle_pace, tuple(warnings)
    )


def _anchor(today: date, months: int, day: int) -> date:
    year, month = divmod(today.year * 12 + today.month - 1 + months, 12)
    return date(year, month + 1, min(day, calendar.monthrange(year, month + 1)[1]))


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _runs_out(left: Decimal, pace: Decimal | None, today: date, reset: date) -> date | None:
    # today + floor(left / pace) days, rounded down so a warning is never late. On or after the
    # reset it lasts the cycle; checking that first keeps the day count small and needs no division
    # by a zero pace.
    if pace is None or left >= pace * (reset - today).days:
        return None
    return today + timedelta(days=int(left // pace))
