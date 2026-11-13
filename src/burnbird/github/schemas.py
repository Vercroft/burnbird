"""The fields kept from each webhook event Burnbird handles, validated with pydantic.

Each model is a subset of GitHub's payload, in GitHub's own shape, so the same model validates the
received body at intake and the stored fields in the worker. Fields a model does not name are
ignored, and only ``model_dump`` output is stored, never the received body.
Payloads: https://docs.github.com/en/webhooks/webhook-events-and-payloads
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict


class Payload(BaseModel):
    """A validated subset of one event's payload."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class InstallationRef(Payload):
    """The installation an event belongs to."""

    id: int


class Repository(Payload):
    """A repository a job ran in."""

    id: int
    name: str
    private: bool


class WorkflowJob(Payload):
    """One attempt of one job, as the ``workflow_job`` event carries it.

    ``workflow_name``, ``runner_id`` and the runner's names are nullable in GitHub's schema.
    """

    id: int
    run_id: int
    run_attempt: int
    workflow_name: str | None
    labels: tuple[str, ...]
    runner_id: int | None
    runner_name: str | None
    runner_group_name: str | None
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    completed_at: AwareDatetime | None
    conclusion: str | None


class WorkflowJobCompleted(Payload):
    """``workflow_job`` with action ``completed``."""

    installation: InstallationRef
    repository: Repository
    workflow_job: WorkflowJob


class Account(Payload):
    """The user or organisation account an installation belongs to."""

    id: int
    login: str
    type: Literal["User", "Organization"]


class Installation(Payload):
    """An installation as the ``installation`` event carries it.

    GitHub's schema gives ``created_at`` as a date-time string or as Unix seconds; both validate.
    """

    id: int
    account: Account
    created_at: AwareDatetime
    suspended_at: AwareDatetime | None = None


class InstallationCreated(Payload):
    """``installation`` with action ``created``."""

    installation: Installation


class InstallationChanged(Payload):
    """``installation`` with action ``deleted``, ``suspend`` or ``unsuspend``."""

    installation: InstallationRef


class RepositoryRef(Payload):
    """A repository named only by its ID."""

    id: int


class RepositoriesRemoved(Payload):
    """``installation_repositories`` with action ``removed``."""

    installation: InstallationRef
    repositories_removed: tuple[RepositoryRef, ...]


# Every (event, action) Burnbird stores, and the model of the fields it keeps. Any other pair is
# answered and dropped.
HANDLED: Final[Mapping[tuple[str, str], type[Payload]]] = MappingProxyType(
    {
        ("workflow_job", "completed"): WorkflowJobCompleted,
        ("installation", "created"): InstallationCreated,
        ("installation", "deleted"): InstallationChanged,
        ("installation", "suspend"): InstallationChanged,
        ("installation", "unsuspend"): InstallationChanged,
        ("installation_repositories", "removed"): RepositoriesRemoved,
    }
)

# The plan a new account starts on, a plan name of the GitHub provider in providers.toml. The
# payload carries no plan, so the account starts on the smallest free allowance, which warns early
# rather than late, until the user chooses their plan.
DEFAULT_PLAN: Final[Mapping[str, str]] = MappingProxyType(
    {"User": "GitHub Free", "Organization": "GitHub Free for organizations"}
)
