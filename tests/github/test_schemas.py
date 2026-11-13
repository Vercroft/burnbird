from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from burnbird.billing.providers import load_table
from burnbird.github import schemas
from burnbird.github.events import HANDLERS

type Body = dict[str, Any]


@pytest.mark.parametrize(
    "created_at", ["2026-11-13T09:00:00Z", "2026-11-13T04:00:00-05:00", 1_794_560_400]
)
def test_an_installation_time_validates_as_a_string_or_unix_seconds(
    created_at: str | int, installation: Callable[..., Body]
) -> None:
    body = installation(action="created", installation_id=1, created_at=created_at)
    parsed = schemas.InstallationCreated.model_validate(body).installation.created_at
    assert parsed.astimezone(UTC) == datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def test_a_time_without_a_timezone_is_refused(installation: Callable[..., Body]) -> None:
    body = installation(action="created", installation_id=1, created_at="2026-11-13T09:00:00")
    with pytest.raises(ValidationError):
        schemas.InstallationCreated.model_validate(body)


def test_a_job_without_a_workflow_name_validates(workflow_job: Callable[..., Body]) -> None:
    body = workflow_job(job_id=1, installation_id=1, repository_id=1, workflow_name=None)
    assert schemas.WorkflowJobCompleted.model_validate(body).workflow_job.workflow_name is None


def test_the_stored_fields_validate_again_unchanged(workflow_job: Callable[..., Body]) -> None:
    parsed = schemas.WorkflowJobCompleted.model_validate(
        workflow_job(job_id=1, installation_id=1, repository_id=1)
    )
    stored = parsed.model_dump(mode="json")
    assert schemas.WorkflowJobCompleted.model_validate(stored) == parsed
    assert set(stored) == {"installation", "repository", "workflow_job"}


def test_the_default_plans_are_plans_in_the_provider_table() -> None:
    github = next(provider for provider in load_table().providers if provider.id == "github")
    plans = {allowance.plan for allowance in github.allowances}
    assert set(schemas.DEFAULT_PLAN) == {"User", "Organization"}
    assert set(schemas.DEFAULT_PLAN.values()) <= plans


def test_every_handled_event_has_a_handler_and_no_other() -> None:
    assert set(HANDLERS) == set(schemas.HANDLED)
