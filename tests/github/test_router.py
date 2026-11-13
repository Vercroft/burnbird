import json
import secrets
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from burnbird.models import WebhookDelivery
from burnbird.web import MAX_BODY_BYTES

type Body = dict[str, Any]

URL = "/github/webhook"
INSTALLATION, REPOSITORY = 4001, 5001


def _stored(session: Session) -> list[WebhookDelivery]:
    session.expire_all()
    return list(session.scalars(select(WebhookDelivery)))


def _job(workflow_job: Callable[..., Body]) -> Body:
    return workflow_job(job_id=1, installation_id=INSTALLATION, repository_id=REPOSITORY)


def _compact(body: Body) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode()


def _padded(body: Body, size: int) -> bytes:
    """The body as JSON of exactly ``size`` bytes, padded with a field no model reads."""
    padding = size - len(_compact({**body, "padding": ""}))
    raw = _compact({**body, "padding": "x" * padding})
    assert len(raw) == size
    return raw


# ---------- the signature comes first ----------


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "other secret",
        "sha1 prefix",
        "body changed",
        "not ascii",
        "not json",
        "unhandled",
    ],
)
def test_a_bad_signature_is_refused_and_nothing_is_stored(
    case: str,
    client: TestClient,
    webhook_secret: bytes,
    signer: Callable[[bytes, bytes], str],
    workflow_job: Callable[..., Body],
    session: Session,
) -> None:
    body, event = _compact(_job(workflow_job)), "workflow_job"
    signature: bytes | None = signer(secrets.token_bytes(32), body).encode()
    if case == "missing":
        signature = None
    elif case == "sha1 prefix":
        signature = signer(webhook_secret, body).replace("sha256=", "sha1=").encode()
    elif case == "body changed":
        signature = signer(webhook_secret, body).encode()
        body += b" "
    elif case == "not ascii":
        signature = b"sha256=\xe9"
    elif case == "not json":
        body = b"not json"
    elif case == "unhandled":
        event = "ping"
    headers = {b"X-GitHub-Delivery": str(uuid.uuid4()).encode(), b"X-GitHub-Event": event.encode()}
    if signature is not None:
        headers[b"X-Hub-Signature-256"] = signature
    response = client.post(URL, content=body, headers=headers)
    assert response.status_code == 403
    assert _stored(session) == []


# ---------- the body limit ----------


def test_a_body_over_the_limit_is_refused_by_its_length(
    deliver: Callable[..., Response], workflow_job: Callable[..., Body], session: Session
) -> None:
    response = deliver("workflow_job", _padded(_job(workflow_job), MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert _stored(session) == []


def test_a_chunked_body_over_the_limit_is_refused_while_it_is_read(
    client: TestClient,
    webhook_secret: bytes,
    signer: Callable[[bytes, bytes], str],
    workflow_job: Callable[..., Body],
    session: Session,
) -> None:
    raw = _padded(_job(workflow_job), MAX_BODY_BYTES + 1)

    def chunks() -> Iterator[bytes]:
        for start in range(0, len(raw), 64 * 1024):
            yield raw[start : start + 64 * 1024]

    response = client.post(
        URL,
        content=chunks(),
        headers={
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-GitHub-Event": "workflow_job",
            "X-Hub-Signature-256": signer(webhook_secret, raw),
        },
    )
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert _stored(session) == []


def test_a_body_of_exactly_the_limit_is_accepted(
    deliver: Callable[..., Response], workflow_job: Callable[..., Body], session: Session
) -> None:
    response = deliver("workflow_job", _padded(_job(workflow_job), MAX_BODY_BYTES))
    assert response.status_code == 202
    assert len(_stored(session)) == 1


# ---------- storing ----------


def test_a_handled_event_stores_only_its_fields(
    deliver: Callable[..., Response], workflow_job: Callable[..., Body], session: Session
) -> None:
    body = _job(workflow_job)
    planted = secrets.token_hex(8)
    body["planted"] = planted
    body["workflow_job"]["planted"] = planted
    delivery = str(uuid.uuid4())
    assert deliver("workflow_job", body, delivery=delivery).status_code == 202
    [stored] = _stored(session)
    assert (str(stored.id), stored.event, stored.action) == (delivery, "workflow_job", "completed")
    assert stored.processed_at is None
    assert stored.attempts == 0
    assert planted not in json.dumps(stored.payload)
    assert set(stored.payload) == {"installation", "repository", "workflow_job"}
    assert "steps" not in stored.payload["workflow_job"]
    assert stored.payload["repository"] == {
        "id": REPOSITORY,
        "name": "example-app",
        "private": True,
    }


def test_a_duplicate_delivery_is_accepted_and_stored_once(
    deliver: Callable[..., Response], workflow_job: Callable[..., Body], session: Session
) -> None:
    delivery = str(uuid.uuid4())
    first = _job(workflow_job)
    again = workflow_job(job_id=2, installation_id=INSTALLATION, repository_id=REPOSITORY)
    assert deliver("workflow_job", first, delivery=delivery).status_code == 202
    assert deliver("workflow_job", again, delivery=delivery).status_code == 202
    [stored] = _stored(session)
    assert stored.payload["workflow_job"]["id"] == 1


# ---------- bad requests ----------


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"X-GitHub-Delivery": ""}, None),
        ({"X-GitHub-Delivery": "not-a-guid"}, None),
        ({"X-GitHub-Event": ""}, None),
        ({}, b"not json"),
        ({}, b"[1, 2]"),
    ],
)
def test_a_bad_header_or_body_is_a_bad_request(
    headers: dict[str, str],
    body: bytes | None,
    deliver: Callable[..., Response],
    workflow_job: Callable[..., Body],
    session: Session,
) -> None:
    response = deliver("workflow_job", body or _job(workflow_job), headers=headers)
    assert response.status_code == 400
    assert _stored(session) == []


def test_a_missing_delivery_header_is_a_bad_request(
    client: TestClient,
    webhook_secret: bytes,
    signer: Callable[[bytes, bytes], str],
    workflow_job: Callable[..., Body],
) -> None:
    raw = _compact(_job(workflow_job))
    headers = {
        "X-GitHub-Event": "workflow_job",
        "X-Hub-Signature-256": signer(webhook_secret, raw),
    }
    assert client.post(URL, content=raw, headers=headers).status_code == 400


def test_a_missing_event_header_is_a_bad_request(
    deliver: Callable[..., Response], workflow_job: Callable[..., Body]
) -> None:
    assert deliver(None, _job(workflow_job)).status_code == 400


def test_a_handled_event_of_the_wrong_shape_is_refused_and_logged_without_the_body(
    deliver: Callable[..., Response],
    workflow_job: Callable[..., Body],
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    planted = secrets.token_hex(8)
    body = _job(workflow_job)
    body["workflow_job"]["id"] = planted
    delivery = str(uuid.uuid4())
    response = deliver("workflow_job", body, delivery=delivery)
    assert response.status_code == 400
    assert _stored(session) == []
    assert delivery in caplog.text
    assert planted not in caplog.text


# ---------- events that are not handled ----------


@pytest.mark.parametrize(
    ("event", "action"),
    [
        ("ping", None),
        ("workflow_job", "queued"),
        ("workflow_job", "in_progress"),
        ("workflow_job", "waiting"),
        ("installation", "new_permissions_accepted"),
        ("installation_repositories", "added"),
        ("installation_target", "renamed"),
        ("github_app_authorization", "revoked"),
    ],
)
def test_an_event_that_is_not_handled_is_answered_and_dropped(
    event: str, action: str | None, deliver: Callable[..., Response], session: Session
) -> None:
    body: Body = {"zen": "Keep it logically awesome.", "hook_id": 1}
    if action is not None:
        body["action"] = action
    response = deliver(event, body)
    assert response.status_code == 204
    assert _stored(session) == []
