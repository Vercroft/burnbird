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

The runner size is always named. The top repository is named only for a recipient GitHub says can
see it now. That question is a network call, so the sender asks it before its send
transaction, with no transaction open and no lock held: ``repository_question`` reads what it
needs, and ``ask`` calls GitHub; the answer names the repository only while the row still names
it. Only read, write or admin names the repository; any other
answer or failure leaves it out, the email still goes, and one WARNING gives the outbox ID and an
outcome code, never the login or the repository's name.
"""

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from burnbird.billing import usage
from burnbird.billing.forecast import WarningKind
from burnbird.billing.providers import load_table
from burnbird.billing.usage import AllowanceState
from burnbird.github.client import (
    GithubAnswerError,
    GithubApp,
    InstallationRefusedError,
    RateLimitedError,
)
from burnbird.models import Account, EmailOutbox, GithubUser, Installation, Repository

logger = logging.getLogger(__name__)

TEMPLATE: Final = "run_out_warning"
_KEYS: Final = frozenset({"provider", "kind", "cycle_start", "repository_id", "runner_class"})
_PERCENT: Final = {WarningKind.USED_75: 75, WarningKind.USED_90: 90}
# The permissions that let a user see a repository: GitHub's answer is admin, write, read or none.
SEEING: Final = frozenset({"admin", "write", "read"})


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


def runner_name(provider: str, runner_class: str | None) -> str | None:
    """Return the provider table's name of a runner size, such as "Ubuntu x64 2 vCPU"."""
    return next(
        (r.name for r in load_table().runners if (r.provider, r.id) == (provider, runner_class)),
        None,
    )


@dataclass(frozen=True, slots=True)
class RepositoryQuestion:
    """What ``ask`` asks GitHub: whether this login can see this repository of this account.

    The IDs say which row, recipient and repository it was asked for, so the answer is used only
    while the row still names them.
    """

    repository_id: int
    account_id: int
    user_id: int
    installation_id: int
    account: str = field(repr=False)
    repository: str = field(repr=False)
    login: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RepositoryAnswer:
    """GitHub's answer: the repository's name if the recipient can see it, else None."""

    question: RepositoryQuestion
    name: str | None = field(repr=False)

    def name_for(self, row: EmailOutbox, warning: Checked) -> str | None:
        """Return the name only for the repository, account and recipient it was asked about."""
        asked = self.question
        same = (asked.repository_id, asked.account_id, asked.user_id) == (
            warning.params.repository_id,
            row.account_id,
            row.user_id,
        )
        return self.name if same else None


def repository_question(session: Session, row: EmailOutbox) -> RepositoryQuestion | None:
    """Read what the repository check asks; no lock is taken.

    None when there is nothing to ask: the row is not a run-out warning, its params are not a
    warning's, it names no repository or no runner size the email could name it with, or the
    repository is no longer stored under the account.
    """
    if row.template != TEMPLATE or row.account_id is None:
        return None
    try:
        warning = WarningParams.parse(row.params)
    except InvalidParamsError:
        return None
    if (
        warning.repository_id is None
        or runner_name(warning.provider, warning.runner_class) is None
    ):
        return None
    found = session.execute(
        select(Repository.name, Installation.id, Account.login)
        .join(Installation, Installation.id == Repository.installation_id)
        .join(Account, Account.id == Installation.account_id)
        .where(Repository.id == warning.repository_id, Account.id == row.account_id)
    ).first()
    login = session.scalar(select(GithubUser.login).where(GithubUser.id == row.user_id))
    if found is None or login is None:
        return None
    name, installation_id, account = found
    return RepositoryQuestion(
        warning.repository_id, row.account_id, row.user_id, installation_id, account, name, login
    )


def ask(
    github: GithubApp | None, row_id: uuid.UUID, question: RepositoryQuestion
) -> RepositoryAnswer:
    """Ask GitHub whether the recipient can see the repository now.

    Call it with no transaction open: it waits on the network. A failure logs one WARNING with
    the outbox ID and an outcome code only.
    """
    if github is None:
        code = "no_client"
    else:
        try:
            permission = github.collaborator_permission(
                question.installation_id, question.account, question.repository, question.login
            )
        # Any failure leaves the name out and the email goes: GitHub's answers, the network, and
        # an error of the client's own (a closed client, a URL it refuses, a token it cannot sign).
        except Exception as err:  # noqa: BLE001  # logged as a code only: its text may name it
            code = _failure(err)
        else:
            if permission in SEEING:
                return RepositoryAnswer(question, question.repository)
            code = "permission:none" if permission == "none" else "permission:other"
    logger.warning("email: %s: the repository is not named (%s)", row_id, code)
    return RepositoryAnswer(question, None)


def _failure(err: Exception) -> str:
    match err:
        case RateLimitedError():
            return "rate_limited"
        case InstallationRefusedError():
            return "refused"
        case GithubAnswerError(status=int() as status):
            return f"http:{status}"
        case GithubAnswerError():
            return "network"
        case _:
            return "error"
