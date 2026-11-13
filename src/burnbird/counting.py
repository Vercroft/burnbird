"""What one job is billed: its minutes after the provider's rounding, and their price or weight.

Pure functions, no I/O. Money and weights are Decimal, never float.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from burnbird.classify import Classification
from burnbird.providers import Rounding, load_table

_MICROSECONDS_PER_MINUTE = 60_000_000


@dataclass(frozen=True, slots=True)
class Job:
    """The fields of one job attempt that billing needs, from the jobs API or a workflow_job event.

    Each run attempt is its own job, and each is billed. The time a job waited in the queue is not
    a field, because it is never billed.
    """

    runner_id: int | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class Billed:
    """One job's billed minutes and amount, on the UTC day it completed.

    ``amount`` is dollars for a priced runner and allowance units for a weighted one; it is None
    when the provider is unknown.
    """

    minutes: Decimal
    amount: Decimal | None
    day: date


def billed_minutes(started_at: datetime, completed_at: datetime, rounding: Rounding) -> Decimal:
    """Return the minutes billed for one job; a completion before the start counts as zero."""
    for moment in (started_at, completed_at):
        if moment.tzinfo is None:
            raise ValueError("job timestamps must carry a timezone")
    microseconds = max(0, (completed_at - started_at) // timedelta(microseconds=1))
    match rounding:
        case Rounding.JOB_UP_TO_MINUTE:
            return Decimal(-(-microseconds // _MICROSECONDS_PER_MINUTE))
        case Rounding.NONE:
            return Decimal(microseconds) / _MICROSECONDS_PER_MINUTE


def bill(job: Job, classification: Classification) -> Billed | None:
    """Bill one job; None when no runner took it or it has no start or completion time.

    A job counts whole on the UTC day it completed, so one that crosses a cycle's reset belongs to
    the new cycle. Neither GitHub nor Blacksmith documents which day they bill such a job on.
    """
    if job.runner_id is None or job.started_at is None or job.completed_at is None:
        return None
    providers = {provider.id: provider for provider in load_table().providers}
    provider = providers.get(classification.provider)
    rounding = provider.rounding if provider is not None else Rounding.NONE
    minutes = billed_minutes(job.started_at, job.completed_at, rounding)
    rate = (
        classification.usd_per_minute
        if classification.usd_per_minute is not None
        else classification.weight
    )
    amount = None if rate is None else minutes * rate
    return Billed(minutes, amount, job.completed_at.astimezone(UTC).date())


def per_day(billed: Iterable[Billed]) -> dict[date, Decimal]:
    """Sum the amounts per UTC day, leaving out jobs with no amount."""
    daily: dict[date, Decimal] = {}
    for item in billed:
        if item.amount is not None:
            daily[item.day] = daily.get(item.day, Decimal(0)) + item.amount
    return daily
