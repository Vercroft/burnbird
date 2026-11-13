"""The provider table: the runners a job can run on, and what a minute on each costs.

The facts are data in ``providers.toml``, shipped inside the package. They are validated when the
table loads, so a bad row fails at once with its name instead of turning into a wrong number later.
"""

import functools
import re
import tomllib
from collections.abc import Set
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from importlib import resources
from typing import Any

GITHUB = "github"
BLACKSMITH = "blacksmith"
SELF_HOSTED = "self-hosted"

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class ProviderTableError(Exception):
    """The provider table is invalid; the message names the row."""


class Rounding(StrEnum):
    """How a provider rounds the time of each job."""

    JOB_UP_TO_MINUTE = "job-up-to-minute"
    NONE = "none"


class Kind(StrEnum):
    """GitHub's two kinds of hosted runner, priced and billed differently."""

    STANDARD = "standard"
    LARGER = "larger"


@dataclass(frozen=True, slots=True)
class Allowance:
    """The free usage a provider includes on one plan."""

    plan: str
    amount: Decimal
    unit: str
    period: str


@dataclass(frozen=True, slots=True)
class Provider:
    """A provider of runners, its rounding rule and its allowances."""

    id: str
    name: str
    rounding: Rounding
    estimate: bool
    allowances: tuple[Allowance, ...]
    sources: tuple[str, ...]
    read: date


@dataclass(frozen=True, slots=True)
class RunnerClass:
    """One runner size: the label patterns that select it and its price or weight per minute."""

    provider: str
    id: str
    name: str
    kind: Kind | None
    labels: tuple[str, ...]
    usd_per_minute: Decimal | None
    weight: Decimal | None
    sources: tuple[str, ...]
    read: date


@dataclass(frozen=True, slots=True)
class ProviderTable:
    """Every provider and every runner size, in table order."""

    providers: tuple[Provider, ...]
    runners: tuple[RunnerClass, ...]


@functools.cache
def load_table() -> ProviderTable:
    """Read and validate the table shipped in the package."""
    text = (
        resources.files("burnbird.billing").joinpath("providers.toml").read_text(encoding="utf-8")
    )
    return parse_table(text)


def parse_table(text: str) -> ProviderTable:
    """Parse and validate a provider table; raise ProviderTableError naming the bad row."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ProviderTableError(f"table: not TOML ({err})") from err
    _check_keys(data, "table", required={"provider", "runner"})
    providers: dict[str, Provider] = {}
    for position, value in enumerate(_tables(data, "provider", "table"), start=1):
        provider = _provider(value, position)
        if provider.id in providers:
            raise ProviderTableError(f"provider {provider.id}: duplicate")
        providers[provider.id] = provider
    runners: dict[tuple[str, str], RunnerClass] = {}
    for position, value in enumerate(_tables(data, "runner", "table"), start=1):
        runner = _runner(value, position, providers)
        key = (runner.provider, runner.id)
        if key in runners:
            raise ProviderTableError(f"runner {runner.provider}/{runner.id}: duplicate")
        runners[key] = runner
    return ProviderTable(tuple(providers.values()), tuple(runners.values()))


def _provider(row: dict[str, Any], position: int) -> Provider:
    where = f"provider {row['id']}" if isinstance(row.get("id"), str) else f"provider {position}"
    _check_keys(
        row,
        where,
        required={"id", "name", "rounding", "estimate", "sources", "read"},
        optional={"allowance"},
    )
    allowances = tuple(
        _allowance(value, f"{where} allowance {index}")
        for index, value in enumerate(_tables(row, "allowance", where, default=[]), start=1)
    )
    return Provider(
        id=_string(row, "id", where),
        name=_string(row, "name", where),
        rounding=_choice(Rounding, row, "rounding", where),
        estimate=_bool(row, "estimate", where),
        allowances=allowances,
        sources=_sources(row, where),
        read=_read(row, where),
    )


def _allowance(row: dict[str, Any], where: str) -> Allowance:
    _check_keys(row, where, required={"plan", "amount", "unit", "period"})
    return Allowance(
        plan=_string(row, "plan", where),
        amount=_decimal(row, "amount", where),
        unit=_string(row, "unit", where),
        period=_string(row, "period", where),
    )


def _runner(row: dict[str, Any], position: int, providers: dict[str, Provider]) -> RunnerClass:
    named = isinstance(row.get("provider"), str) and isinstance(row.get("id"), str)
    where = f"runner {row['provider']}/{row['id']}" if named else f"runner {position}"
    _check_keys(
        row,
        where,
        required={"provider", "id", "name", "labels", "sources", "read"},
        optional={"kind", "usd_per_minute", "weight"},
    )
    provider = _string(row, "provider", where)
    if provider not in providers:
        raise ProviderTableError(f"{where}: provider {provider!r} is not declared")
    if ("usd_per_minute" in row) == ("weight" in row):
        raise ProviderTableError(f"{where}: needs exactly one of usd_per_minute and weight")
    if ("kind" in row) != (provider == GITHUB):
        raise ProviderTableError(
            f"{where}: kind is required on {GITHUB} rows and allowed only there"
        )
    return RunnerClass(
        provider=provider,
        id=_string(row, "id", where),
        name=_string(row, "name", where),
        kind=_choice(Kind, row, "kind", where) if "kind" in row else None,
        labels=_strings(row, "labels", where),
        usd_per_minute=_decimal(row, "usd_per_minute", where) if "usd_per_minute" in row else None,
        weight=_decimal(row, "weight", where) if "weight" in row else None,
        sources=_sources(row, where),
        read=_read(row, where),
    )


def _check_keys(
    row: dict[str, Any], where: str, required: Set[str], optional: Set[str] = frozenset()
) -> None:
    if missing := required - row.keys():
        raise ProviderTableError(f"{where}: missing {', '.join(sorted(missing))}")
    if unknown := row.keys() - required - optional:
        raise ProviderTableError(f"{where}: unknown key {', '.join(sorted(unknown))}")


def _tables(
    row: dict[str, Any], key: str, where: str, default: list[Any] | None = None
) -> list[dict[str, Any]]:
    value = row.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProviderTableError(f"{where}: {key} must be an array of tables")
    return value


def _string(row: dict[str, Any], key: str, where: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value:
        raise ProviderTableError(f"{where}: {key} must be a non-empty string")
    return value


def _strings(row: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = row[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ProviderTableError(f"{where}: {key} must be a list of non-empty strings")
    return tuple(value)


def _bool(row: dict[str, Any], key: str, where: str) -> bool:
    value = row[key]
    if not isinstance(value, bool):
        raise ProviderTableError(f"{where}: {key} must be true or false")
    return value


def _decimal(row: dict[str, Any], key: str, where: str) -> Decimal:
    text = _string(row, key, where)
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = Decimal("NaN")
    if not value.is_finite() or value < 0:
        raise ProviderTableError(f"{where}: {key} {text!r} is not a finite, non-negative decimal")
    return value


def _choice[E: StrEnum](choices: type[E], row: dict[str, Any], key: str, where: str) -> E:
    text = _string(row, key, where)
    try:
        return choices(text)
    except ValueError as err:
        allowed = ", ".join(choice.value for choice in choices)
        raise ProviderTableError(f"{where}: {key} {text!r} is not one of {allowed}") from err


def _sources(row: dict[str, Any], where: str) -> tuple[str, ...]:
    urls = _strings(row, "sources", where)
    if not urls or not all(url.startswith("https://") for url in urls):
        raise ProviderTableError(f"{where}: sources must be one or more https:// links")
    return urls


def _read(row: dict[str, Any], where: str) -> date:
    text = _string(row, "read", where)
    try:
        read = date.fromisoformat(text) if _DATE.fullmatch(text) else None
    except ValueError:
        read = None
    if read is None:
        raise ProviderTableError(f"{where}: read {text!r} is not a YYYY-MM-DD date")
    return read
