"""``POST /email/webhook``: the provider's hard bounces pause an address; nothing else changes.

Every body is invented in the provider's documented shape; references are made at run time, the
key comes from ``secrets`` and addresses are on example.invalid.
"""

import dataclasses
import json
import logging
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.config import AuthSettings
from burnbird.email import config
from burnbird.email.webhook import EmailWebhookState
from burnbird.github.config import ConfigError
from burnbird.models import EmailOutbox, EmailPreference, GithubUser, UserEmail
from burnbird.web import MAX_BODY_BYTES, create_app
from tests.email.examples import webhook_event
from tests.email.test_webhook_signature import sign

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
URL = "/email/webhook"
USER = 7801


def _address(name: str) -> str:
    return name + "@" + "example.invalid"


ADDRESS = _address("reader")


@pytest.fixture
def key() -> bytes:
    return secrets.token_urlsafe(48).encode()


@dataclasses.dataclass
class Clock:
    """The webhook's clock; a test moves it."""

    at: datetime = NOW


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(
    key: bytes, clock: Clock, sessions: sessionmaker[Session], auth_settings: AuthSettings
) -> Iterator[TestClient]:
    app = create_app(secrets.token_bytes(32), sessions, auth_settings, email_webhook_key=key)
    state = app.state.email_webhook
    assert isinstance(state, EmailWebhookState)
    app.state.email_webhook = dataclasses.replace(state, now=lambda: clock.at)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def reference(session: Session) -> str:
    """A user with an address and the default choices, and one sent usage email."""
    session.add(GithubUser(id=USER, login="example-reader", last_sign_in_at=NOW))
    session.flush()
    session.add(UserEmail(user_id=USER, address=ADDRESS, read_at=NOW))
    session.add_all(
        EmailPreference(user_id=USER, kind=kind, enabled=kind == "usage_warnings", updated_at=NOW)
        for kind in ("usage_warnings", "weekly_summary", "news")
    )
    row_id = uuid.uuid4()
    session.add(
        EmailOutbox(
            id=row_id,
            user_id=USER,
            kind="usage_warnings",
            template="run_out_warning",
            dedupe_key=f"example:{row_id}",
            params={},
            created_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(days=1),
            state="sent",
            finished_at=NOW - timedelta(hours=1),
        )
    )
    session.flush()
    return str(row_id)


def _post(client: TestClient, key: bytes, body: bytes, *, at: datetime = NOW) -> Any:
    return client.post(
        URL,
        content=body,
        headers={"producer-signature": sign(key, body, at), "Content-Type": "application/json"},
    )


def _json(data: object) -> bytes:
    return json.dumps(data).encode()


def _pause(session: Session) -> tuple[datetime | None, str | None]:
    session.expire_all()
    email = session.get_one(UserEmail, USER)
    return email.suppressed_at, email.suppressed_reason


# ---------- the signature comes first ----------


@pytest.mark.parametrize("case", ["missing", "other key", "stale", "not json"])
def test_a_bad_signature_is_refused_and_nothing_changes(
    case: str,
    client: TestClient,
    key: bytes,
    reference: str,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _json(webhook_event("hardbounce", reference, ADDRESS))
    header: dict[str, str] = {"producer-signature": sign(secrets.token_bytes(32), body)}
    if case == "missing":
        header = {}
    elif case == "stale":
        header = {"producer-signature": sign(key, body, NOW - timedelta(minutes=6))}
    elif case == "not json":
        body = b"not json"
    caplog.set_level(logging.INFO)
    answer = client.post(URL, content=body, headers={**header, "Content-Type": "text/plain"})
    assert answer.status_code == 403
    assert _pause(session) == (None, None)
    # The refusal says what came, never what it held.
    assert f"text/plain, {len(body)} bytes" in caplog.text
    assert ADDRESS not in caplog.text


def test_without_a_key_there_is_no_route(
    sessions: sessionmaker[Session], auth_settings: AuthSettings
) -> None:
    with TestClient(create_app(secrets.token_bytes(32), sessions, auth_settings)) as client:
        assert client.post(URL, content=b"{}").status_code == 404


def test_an_oversized_body_is_refused_before_the_signature(client: TestClient, key: bytes) -> None:
    body = b"x" * (MAX_BODY_BYTES + 1)
    assert _post(client, key, body).status_code == 413


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'"text"'], ids=["not json", "array", "text"]
)
def test_a_signed_body_that_is_not_a_json_object_is_refused(
    client: TestClient, key: bytes, body: bytes
) -> None:
    assert _post(client, key, body).status_code == 400


def test_a_signed_body_of_another_shape_is_refused_by_field_only(
    client: TestClient, key: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    body = _json({"event_message": [{"email_info": {"client_reference": 5}, "event_data": 7}]})
    assert _post(client, key, body).status_code == 400
    assert "event_data" in caplog.text


# ---------- a hard bounce ----------


def test_a_hard_bounce_pauses_the_address_and_logs_no_address(
    client: TestClient,
    key: bytes,
    reference: str,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    answer = _post(client, key, _json(webhook_event("hardbounce", reference, ADDRESS.upper())))
    assert (answer.status_code, answer.content) == (200, b"")
    assert _pause(session) == (NOW, "hard_bounce")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [f"email webhook: hardbounce for outbox {reference}"]
    assert ADDRESS not in caplog.text.lower()


def test_a_repeated_bounce_keeps_the_first_pause(
    client: TestClient, clock: Clock, key: bytes, reference: str, session: Session
) -> None:
    body = _json(webhook_event("hardbounce", reference, ADDRESS))
    _post(client, key, body)
    clock.at = NOW + timedelta(minutes=1)
    assert _post(client, key, body, at=clock.at).status_code == 200
    assert _pause(session) == (NOW, "hard_bounce")


def test_a_bounce_for_an_address_the_user_no_longer_has_changes_nothing(
    client: TestClient, key: bytes, reference: str, session: Session
) -> None:
    body = _json(webhook_event("hardbounce", reference, _address("older")))
    assert _post(client, key, body).status_code == 200
    assert _pause(session) == (None, None)


@pytest.mark.parametrize("kind", ["softbounce", "email_open"])
def test_other_events_are_answered_and_ignored(
    client: TestClient, key: bytes, reference: str, session: Session, kind: str
) -> None:
    assert _post(client, key, _json(webhook_event(kind, reference, ADDRESS))).status_code == 200
    assert _pause(session) == (None, None)


@pytest.mark.parametrize("unknown", ["uuid", "not a uuid", "none"])
def test_an_unknown_reference_is_answered_and_ignored(
    client: TestClient, key: bytes, reference: str, session: Session, unknown: str
) -> None:
    other = {"uuid": str(uuid.uuid4()), "not a uuid": "example-reference", "none": None}[unknown]
    event = webhook_event("hardbounce", reference, ADDRESS)
    event["event_message"][0]["email_info"]["client_reference"] = other
    assert _post(client, key, _json(event)).status_code == 200
    assert _pause(session) == (None, None)


# ---------- the setting ----------


def test_the_key_setting_is_optional_but_never_empty(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(config.WEBHOOK_KEY_FILE_VARIABLE, raising=False)
    assert config.webhook_key() is None
    key_file = tmp_path / "key"
    key_file.write_bytes(b"example-key\n")
    monkeypatch.setenv(config.WEBHOOK_KEY_FILE_VARIABLE, str(key_file))
    assert config.webhook_key() == b"example-key"
    key_file.write_bytes(b"")
    with pytest.raises(ConfigError, match=config.WEBHOOK_KEY_FILE_VARIABLE):
        config.webhook_key()


def test_an_ignored_event_of_another_shape_does_not_drop_a_bounce(
    client: TestClient, key: bytes, reference: str, session: Session
) -> None:
    # Only the events acted on are read closely; an open with odd details, or a reference that is
    # a number, is ignored on its own and the bounce beside it still counts.
    event = webhook_event("hardbounce", reference, ADDRESS)
    event["event_message"].append(
        {
            "email_info": {"client_reference": 12345},
            "event_data": [{"object": "email_open", "details": "not a list"}],
        }
    )
    assert _post(client, key, _json(event)).status_code == 200
    assert _pause(session) == (NOW, "hard_bounce")


def test_addresses_are_compared_by_the_database_s_own_lower_case(
    client: TestClient, key: bytes, reference: str, session: Session
) -> None:
    # Python lower-cases a dotted capital I to two characters and the database to one: both sides
    # are lower-cased by the database, so they agree.
    email = session.get_one(UserEmail, USER)
    email.address = "İx" + "@" + "example.invalid"
    session.flush()
    reported = "İX" + "@" + "example.invalid"
    assert (
        _post(client, key, _json(webhook_event("hardbounce", reference, reported))).status_code
        == 200
    )
    assert _pause(session) == (NOW, "hard_bounce")
