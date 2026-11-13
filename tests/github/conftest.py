"""The webhook app on the rolled-back test connection, a random secret, and invented payloads.

The payloads are invented data shaped like GitHub's documented examples, with a few of the fields
Burnbird ignores, so the tests show those are dropped. No real account, repository or runner.
Signatures are computed here with hmac directly, independent of the code under test.
"""

import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.config import AuthSettings
from burnbird.web import create_app

type Body = dict[str, Any]


@pytest.fixture
def webhook_secret() -> bytes:
    return secrets.token_hex(32).encode()


@pytest.fixture
def client(
    webhook_secret: bytes, sessions: sessionmaker[Session], auth_settings: AuthSettings
) -> Iterator[TestClient]:
    with TestClient(create_app(webhook_secret, sessions, auth_settings)) as client:
        yield client


def sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture
def deliver(client: TestClient, webhook_secret: bytes) -> Callable[..., Response]:
    """POST a delivery, signed with the app's secret unless ``signature`` is given."""

    def post(
        event: str | None,
        body: Body | bytes,
        *,
        delivery: str | None = None,
        signature: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        sent = {
            "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        if event is not None:
            sent["X-GitHub-Event"] = event
        sent["X-Hub-Signature-256"] = signature or sign(webhook_secret, raw)
        sent.update(headers or {})
        return client.post("/github/webhook", content=raw, headers=sent)

    return post


def _account(account_id: int, login: str, kind: str) -> Body:
    return {
        "login": login,
        "id": account_id,
        "node_id": "N_example",
        "type": kind,
        "site_admin": False,
    }


def workflow_job_body(
    *,
    job_id: int,
    installation_id: int,
    repository_id: int,
    action: str = "completed",
    run_attempt: int = 1,
    private: bool = True,
    labels: Sequence[str] = ("ubuntu-latest",),
    runner_group_name: str | None = "GitHub Actions",
    runner_id: int | None = 7,
    workflow_name: str | None = "ci",
    started_at: str | None = "2026-11-13T10:01:00Z",
    completed_at: str | None = "2026-11-13T10:03:30Z",
    conclusion: str | None = "success",
) -> Body:
    return {
        "action": action,
        "workflow_job": {
            "id": job_id,
            "run_id": job_id * 10,
            "run_attempt": run_attempt,
            "node_id": "N_example",
            "head_sha": "0" * 40,
            "status": "completed",
            "conclusion": conclusion,
            "created_at": "2026-11-13T10:00:00Z",
            "started_at": started_at,
            "completed_at": completed_at,
            "name": "test",
            "steps": [{"name": "Set up job", "status": "completed", "number": 1}],
            "labels": list(labels),
            "runner_id": runner_id,
            "runner_name": None if runner_id is None else f"GitHub Actions {runner_id}",
            "runner_group_id": 2,
            "runner_group_name": runner_group_name,
            "workflow_name": workflow_name,
            "head_branch": "main",
        },
        "repository": {
            "id": repository_id,
            "node_id": "N_example",
            "name": "example-app",
            "full_name": "example-org/example-app",
            "private": private,
        },
        "installation": {"id": installation_id, "node_id": "N_example"},
        "sender": _account(3009, "example-person", "User"),
    }


def installation_body(
    *,
    action: str,
    installation_id: int,
    account_id: int = 3001,
    login: str = "example-org",
    kind: str = "Organization",
    created_at: str | int = "2026-11-13T09:00:00Z",
) -> Body:
    return {
        "action": action,
        "installation": {
            "id": installation_id,
            "account": _account(account_id, login, kind),
            "repository_selection": "selected",
            "app_id": 1,
            "target_type": kind,
            "permissions": {"actions": "read", "metadata": "read"},
            "events": ["workflow_job"],
            "created_at": created_at,
            "suspended_at": None,
        },
        "repositories": [
            {
                "id": 5001,
                "node_id": "N_example",
                "name": "example-app",
                "full_name": "example-org/example-app",
                "private": True,
            }
        ],
        "requester": None,
        "sender": _account(3009, "example-person", "User"),
    }


def repositories_removed_body(*, installation_id: int, repository_ids: Sequence[int]) -> Body:
    return {
        "action": "removed",
        "installation": {"id": installation_id},
        "repository_selection": "selected",
        "repositories_added": [],
        "repositories_removed": [
            {
                "id": repository_id,
                "name": "example-app",
                "full_name": "example-org/example-app",
                "private": True,
            }
            for repository_id in repository_ids
        ],
        "requester": None,
        "sender": _account(3009, "example-person", "User"),
    }


@pytest.fixture
def workflow_job() -> Callable[..., Body]:
    return workflow_job_body


@pytest.fixture
def installation() -> Callable[..., Body]:
    return installation_body


@pytest.fixture
def repositories_removed() -> Callable[..., Body]:
    return repositories_removed_body


@pytest.fixture
def signer() -> Callable[[bytes, bytes], str]:
    return sign
