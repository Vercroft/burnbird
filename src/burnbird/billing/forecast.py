"""How much of an allowance is used, how fast, and the day it runs out.

Pure functions, no I/O. Days are UTC dates; ``today`` is the caller's UTC date and counts as a
partial day: in what is used, never in a pace. Warnings are data; the pages word them.

The pace is one rule, for the page and the email alike: the last 7 full UTC days on or after the
day the data is complete from (``first_complete_day``), reaching back across the cycle start, with
the single busiest day left out and the mean of the rest. With fewer than 4 such days there is no
pace, so a fresh install, or one busy first day, forecasts no run-out. It replaced a plain mean of
the cycle's last 7 full days (2026-11-18), which one busy day turned into a false run-out.
"""

import calendar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import accumulate

# The full days a pace looks back over, and the fewest of them on or after the day the data is
# complete from that give a pace at all.
PACE_DAYS = 7
PACE_MIN_DAYS = 4


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

    ``left`` is negative once the allowance is used up. ``pace`` is the module's rule, None with
    fewer than 4 days. ``pace_days`` is how many of the last 7 full days it looked at: those on or
    after the day the data is complete from. ``cycle_pace``, for the page only, is the mean over
    every full day of the cycle on or after that day, None without one. A run-out date is None
    when the allowance lasts the cycle or there is no pace; once used up it is the day it ran out.
    """

    start: date
    reset: date
    used: Decimal
    left: Decimal
    pace: Decimal | None
    pace_days: int
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


def first_complete_day(
    installed_at: datetime, backfilled: bool, cycle_start: date, data_deleted_at: datetime | None
) -> date:
    """Return the first UTC day whose jobs are all stored, from what the backfill does.

    Webhooks bring every job from the installation on, so the day after it is complete. The first
    sync queues every repository for a backfill from the earlier of the account's two cycle starts
    (``cycle_start``); once every repository is backfilled (``backfilled``), the data is complete
    from that start if it is earlier. Never before the day after Delete my data, which deleted
    every job before it.
    """
    for moment in (installed_at, data_deleted_at):
        if moment is not None and moment.tzinfo is None:
            raise ValueError("times must carry a timezone")
    first = _utc_day(installed_at) + timedelta(days=1)
    if backfilled:
        first = min(first, cycle_start)
    if data_deleted_at is not None:
        first = max(first, _utc_day(data_deleted_at) + timedelta(days=1))
    return first


def forecast(
    daily: Mapping[date, Decimal],
    allowance: Decimal,
    cycle_day: int,
    today: date,
    *,
    complete_from: date,
) -> Forecast:
    """Forecast one allowance from its use per UTC day.

    ``daily`` may hold days before the cycle start: they count only in the pace. ``complete_from``
    is the first day whose data is complete (``first_complete_day``); no day before it counts in a
    pace, though its use counts as used.
    """
    if allowance <= 0:
        raise ValueError("allowance must be positive")
    start, reset = cycle(today, cycle_day)
    days = [start + timedelta(days=n) for n in range((today - start).days + 1)]
    use = [daily.get(day, Decimal(0)) for day in days]
    used = sum(use, Decimal(0))
    left = allowance - used
    window = [today - timedelta(days=n) for n in range(PACE_DAYS, 0, -1)]
    counted = [daily.get(day, Decimal(0)) for day in window if day >= complete_from]
    pace = _pace(counted)
    cycle_pace = _mean(
        [total for day, total in zip(days[:-1], use[:-1], strict=True) if day >= complete_from]
    )
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
        start,
        reset,
        used,
        left,
        pace,
        len(counted),
        cycle_pace,
        runs_out,
        by_cycle_pace,
        tuple(warnings),
    )


def _anchor(today: date, months: int, day: int) -> date:
    year, month = divmod(today.year * 12 + today.month - 1 + months, 12)
    return date(year, month + 1, min(day, calendar.monthrange(year, month + 1)[1]))


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _pace(days: Sequence[Decimal]) -> Decimal | None:
    # One busy day (a release, a first day's backfill) would otherwise forecast a false run-out.
    # Only one day is left out: when two share the top, the other still counts.
    if len(days) < PACE_MIN_DAYS:
        return None
    return (sum(days, Decimal(0)) - max(days)) / (len(days) - 1)


def _utc_day(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


def _runs_out(left: Decimal, pace: Decimal | None, today: date, reset: date) -> date | None:
    # today + floor(left / pace) days, rounded down so a warning is never late. On or after the
    # reset it lasts the cycle; checking that first keeps the day count small and needs no division
    # by a zero pace.
    if pace is None or left >= pace * (reset - today).days:
        return None
    return today + timedelta(days=int(left // pace))
