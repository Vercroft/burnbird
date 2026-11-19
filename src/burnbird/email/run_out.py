"""Run-out warnings at send: what a queued row asks for, and whether it is still true.

A ``run_out_warning`` row's ``params`` hold only the provider, the warning kind, the cycle start it
was queued for, and the cycle's top repository and runner size, as IDs. At send, after the caps,
``checked`` reads ``usage.allowance_state`` once: it is both the re-check and the source of every
date and number the email gives.

- A row queued for another cycle than the state's (a new cycle, or a changed cycle day) is
  ``expired``.
- A warning the state no longer bears out is ``no_longer_true``: a percentage now under its
  threshold or a ran-out no longer used up (jobs deleted), a run-out the forecast no longer makes
  (jobs deleted, the plan or cycle day changed, a new day moved the pace).

Params that are not a run-out warning's fail the row: a kind its provider does not list in
``email_warnings``, an unknown key, or a value of the wrong type.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sqlalchemy.orm import Session

from burnbird.billing import usage
from burnbird.billing.forecast import WarningKind
from burnbird.billing.providers import load_table
from burnbird.billing.usage import AllowanceState
from burnbird.models import EmailOutbox

TEMPLATE: Final = "run_out_warning"
_KEYS: Final = frozenset({"provider", "kind", "cycle_start", "repository_id", "runner_class"})
_PERCENT: Final = {WarningKind.USED_75: 75, WarningKind.USED_90: 90}


class SkippedError(Exception):
    """The warning is not sent, for ``reason``: an outcome code."""

    def __init__(self, reason: str) -> None:
        """Keep the outcome code."""
        super().__init__(reason)
        self.reason = reason


class InvalidParamsError(Exception):
    """The row's params are not a run-out warning's; the message names no value."""


@dataclass(frozen=True, slots=True)
class WarningParams:
    """What a queued run-out warning holds: kinds, a date and IDs only."""

    provider: str
    kind: WarningKind
    cycle_start: date
    repository_id: int | None
    runner_class: str | None

    @classmethod
    def parse(cls, params: object) -> WarningParams:
        """Read a row's params; raise ``InvalidParamsError`` unless they are a warning's.

        ``params`` is whatever the JSONB column holds, so not always an object.
        """
        if not isinstance(params, Mapping) or set(params) != _KEYS:
            raise InvalidParamsError("the params hold other keys than a run-out warning's")
        provider = next((p for p in load_table().providers if p.id == params["provider"]), None)
        if provider is None or params["kind"] not in provider.email_warnings:
            raise InvalidParamsError("the warning is not one its provider lists")
        try:
            cycle_start = date.fromisoformat(params["cycle_start"])
        except TypeError, ValueError:
            raise InvalidParamsError("the cycle start is not a date") from None
        repository_id, runner_class = params["repository_id"], params["runner_class"]
        if repository_id is not None and (
            not isinstance(repository_id, int) or isinstance(repository_id, bool)
        ):
            raise InvalidParamsError("the repository is not an ID")
        if runner_class is not None and not isinstance(runner_class, str):
            raise InvalidParamsError("the runner size is not a name")
        return cls(
            provider.id, WarningKind(params["kind"]), cycle_start, repository_id, runner_class
        )


@dataclass(frozen=True, slots=True)
class Checked:
    """A warning still to send: its params, the allowance's state now, and its run-out date.

    ``on`` is the day the allowance runs out (runs-out-before-reset) or ran out (ran-out); None for
    a percentage warning.
    """

    params: WarningParams
    state: AllowanceState
    on: date | None


def checked(session: Session, row: EmailOutbox, now: datetime) -> Checked:
    """Return the row's warning with the allowance's state now, or raise ``SkippedError``.

    Raise ``InvalidParamsError`` when the params are not a run-out warning's.
    """
    warning = WarningParams.parse(row.params)
    if row.account_id is None:
        raise InvalidParamsError("a run-out warning names no account")
    state = usage.allowance_state(session, row.account_id, warning.provider, now)
    if state is None:
        raise SkippedError("no_longer_true")
    if state.forecast.start != warning.cycle_start:
        raise SkippedError("expired")
    if (percent := _PERCENT.get(warning.kind)) is not None:
        if state.forecast.used * 100 < state.allowance.amount * percent:
            raise SkippedError("no_longer_true")
        return Checked(warning, state, None)
    on = next((w.on for w in state.forecast.warnings if w.kind == warning.kind), None)
    if on is None:
        raise SkippedError("no_longer_true")
    return Checked(warning, state, on)
