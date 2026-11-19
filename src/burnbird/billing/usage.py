"""Each allowance's use per UTC day, read from the stored jobs, and its forecast.

The one place that reads jobs for an allowance: the page and the run-out email both call
``allowance_state``, so they share ``burnbird.billing.forecast``'s pace rule and its number.

Both queries read one installation's jobs through its repositories: an index scan of
``ix_repository_installation_id``, then of ``ix_job_repository_id_completed_at`` for each
repository's time range, never a sequential scan of ``job``. They take no lock.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import (
    ColumnElement,
    Date,
    Numeric,
    cast,
    desc,
    func,
    literal_column,
    select,
    type_coerce,
)
from sqlalchemy.orm import Session

from burnbird.billing.forecast import (
    PACE_DAYS,
    Forecast,
    cycle,
    first_complete_day,
    forecast,
)
from burnbird.billing.providers import BLACKSMITH, GITHUB, Allowance, Provider, load_table
from burnbird.models import Account, Installation, Job, Repository

# A job counts on the UTC day it completed (``counting.bill``). A literal, not a parameter, so the
# GROUP BY repeats the same expression.
_DAY = cast(func.timezone(literal_column("'UTC'"), Job.completed_at), Date)
# Never NULL: only jobs with an amount are summed.
_TOTAL = type_coerce(func.sum(Job.amount), Numeric())


@dataclass(frozen=True, slots=True)
class Usage:
    """How much of a provider's allowance one repository used on one runner size."""

    repository_id: int
    runner_class: str | None
    amount: Decimal


@dataclass(frozen=True, slots=True)
class AllowanceState:
    """One account's allowance now: the allowance, where its data is complete, its forecast."""

    provider: str
    allowance: Allowance
    complete_from: date
    forecast: Forecast


def daily_totals(
    session: Session,
    installation_id: int,
    providers: Collection[str],
    since: date,
    until: date,
) -> dict[str, dict[date, Decimal]]:
    """Sum each provider's amounts per UTC day, from the day ``since`` to before the day ``until``.

    Only the installation's jobs of the providers asked, and only those with an amount: a job of
    an unknown provider, or one no runner took, uses no allowance. A day with no job is absent.
    """
    totals: dict[str, dict[date, Decimal]] = {}
    if not providers:
        return totals
    rows = session.execute(
        select(Job.provider, _DAY, _TOTAL)
        .join(Repository, Repository.id == Job.repository_id)
        .where(*_window(installation_id, since, until), Job.provider.in_(providers))
        .group_by(Job.provider, _DAY)
    )
    for provider, day, amount in rows:
        totals.setdefault(provider, {})[day] = amount
    return totals


def top_usage(
    session: Session, installation_id: int, provider: str, since: date, until: date
) -> tuple[Usage, ...]:
    """Rank each repository and runner size by its use of the provider's allowance, most first.

    Jobs completed from the day ``since`` to before the day ``until``. One that used none of the
    allowance (a public repository's GitHub-hosted minutes) is left out. Ties go to the lower
    repository ID.
    """
    rows = session.execute(
        select(Job.repository_id, Job.runner_class, _TOTAL)
        .join(Repository, Repository.id == Job.repository_id)
        .where(*_window(installation_id, since, until), Job.provider == provider)
        .group_by(Job.repository_id, Job.runner_class)
        .having(_TOTAL > 0)
        .order_by(desc(_TOTAL), Job.repository_id, Job.runner_class)
    )
    return tuple(Usage(repository_id, runner, amount) for repository_id, runner, amount in rows)


def complete_from(session: Session, installation_id: int, today: date) -> date:
    """Return the first UTC day whose jobs are all stored for the installation.

    The rule is ``forecast.first_complete_day``. The backfill starts each repository at the earlier
    of the account's two cycle starts (``github.backfill``), and the data reaches back to it only
    once every repository of the installation is backfilled.
    """
    row = session.execute(
        select(
            Installation.created_at,
            Account.github_cycle_day,
            Account.blacksmith_cycle_day,
            Account.data_deleted_at,
            ~select(Repository.id)
            .where(
                Repository.installation_id == Installation.id,
                Repository.backfilled_at.is_(None),
            )
            .exists(),
        )
        .join(Account, Account.id == Installation.account_id)
        .where(Installation.id == installation_id)
    ).one()
    installed_at, github_day, blacksmith_day, deleted_at, backfilled = row
    start = min(cycle(today, github_day)[0], cycle(today, blacksmith_day)[0])
    return first_complete_day(installed_at, backfilled, start, deleted_at)


def allowance_state(
    session: Session, account_id: int, provider_id: str, now: datetime
) -> AllowanceState | None:
    """Return one allowance's state at ``now``; None without an allowance or an installation.

    GitHub's allowance is the account's chosen plan; another provider has one allowance. Its cycle
    starts on the account's cycle day for that provider. The days read reach back far enough for
    the pace, and never past where the data is complete.
    """
    if now.tzinfo is None:
        raise ValueError("now must carry a timezone")
    provider = _provider(provider_id)
    if not provider.allowances:
        return None
    row = session.execute(
        select(
            Installation.id,
            Account.github_plan,
            Account.github_cycle_day,
            Account.blacksmith_cycle_day,
        )
        .join(Installation, Installation.account_id == Account.id)
        .where(Account.id == account_id)
    ).first()
    if row is None:
        return None
    installation_id, plan, github_day, blacksmith_day = row
    allowance = _allowance(provider, plan, account_id)
    cycle_day = {GITHUB: github_day, BLACKSMITH: blacksmith_day}[provider.id]
    today = now.astimezone(UTC).date()
    complete = complete_from(session, installation_id, today)
    start, _ = cycle(today, cycle_day)
    # Every day of the cycle counts as used; before it, only the complete days of the pace.
    since = min(start, max(complete, today - timedelta(days=PACE_DAYS)))
    daily = daily_totals(
        session, installation_id, (provider.id,), since, today + timedelta(days=1)
    )
    result = forecast(
        daily.get(provider.id, {}), allowance.amount, cycle_day, today, complete_from=complete
    )
    return AllowanceState(provider.id, allowance, complete, result)


def _window(installation_id: int, since: date, until: date) -> tuple[ColumnElement[bool], ...]:
    return (
        Repository.installation_id == installation_id,
        Job.amount.is_not(None),
        Job.completed_at >= _midnight(since),
        Job.completed_at < _midnight(until),
    )


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), UTC)


def _provider(provider_id: str) -> Provider:
    for provider in load_table().providers:
        if provider.id == provider_id:
            return provider
    raise ValueError(f"provider {provider_id!r} is not in the provider table")


def _allowance(provider: Provider, plan: str, account_id: int) -> Allowance:
    if provider.id == GITHUB:
        for allowance in provider.allowances:
            if allowance.plan == plan:
                return allowance
        raise ValueError(
            f"account {account_id}: GitHub plan {plan!r} is not in the provider table"
        )
    if len(provider.allowances) != 1:
        raise ValueError(f"provider {provider.id}: needs exactly one allowance")
    return provider.allowances[0]
