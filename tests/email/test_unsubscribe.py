"""One-click unsubscribe (RFC 8058): GET only shows a button, POST turns the kind off.

The POST needs no cookie and no CSRF token: the token in the path is the only proof, and an
unknown one gets the same answer. Every address and ID is invented.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from burnbird.models import EmailConsent, EmailOutbox, EmailPreference, GithubUser

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
USER = 7701


@pytest.fixture
def token(session: Session) -> str:
    """A user with warnings on, and a sent usage email whose link holds this token."""
    session.add(GithubUser(id=USER, login="example-reader", last_sign_in_at=NOW))
    session.flush()
    session.add(EmailPreference(user_id=USER, kind="usage_warnings", enabled=True, updated_at=NOW))
    token = secrets.token_urlsafe(32)
    session.add(
        EmailOutbox(
            id=uuid.uuid4(),
            user_id=USER,
            kind="usage_warnings",
            template="run_out_warning",
            dedupe_key="example:unsubscribe",
            params={},
            created_at=NOW,
            expires_at=NOW + timedelta(days=1),
            state="sent",
            finished_at=NOW,
            unsubscribe_sha256=hashlib.sha256(token.encode()).digest(),
        )
    )
    session.flush()
    return token


def _state(session: Session) -> tuple[bool, list[str]]:
    session.expire_all()
    enabled = session.scalar(
        select(EmailPreference.enabled).where(
            EmailPreference.user_id == USER, EmailPreference.kind == "usage_warnings"
        )
    )
    sources = list(
        session.scalars(
            select(EmailConsent.source)
            .where(EmailConsent.user_id == USER)
            .order_by(EmailConsent.id)
        )
    )
    assert enabled is not None
    return enabled, sources


def test_get_shows_a_button_and_changes_nothing(
    client: TestClient, session: Session, token: str
) -> None:
    before = session.scalar(select(func.count()).select_from(EmailConsent))
    answer = client.get(f"/email/unsubscribe/{token}")
    assert answer.status_code == 200
    assert f'action="/email/unsubscribe/{token}"' in answer.text
    assert 'method="post"' in answer.text
    assert _state(session) == (True, [])
    assert session.scalar(select(func.count()).select_from(EmailConsent)) == before


@pytest.mark.parametrize(
    "body",
    [
        {"data": {"List-Unsubscribe": "One-Click"}},
        {"files": {"List-Unsubscribe": (None, "One-Click")}},
        {},
        {"content": b"anything at all"},
    ],
    ids=["urlencoded", "multipart", "empty", "other content"],
)
def test_post_turns_warnings_off_once_with_no_cookie_and_no_redirect(
    client: TestClient, session: Session, token: str, body: dict[str, Any]
) -> None:
    client.cookies.clear()
    answer = client.post(
        f"/email/unsubscribe/{token}", headers={"Origin": "https://mail.example.invalid"}, **body
    )
    assert answer.status_code == 200
    assert "location" not in answer.headers
    assert "set-cookie" not in answer.headers
    assert _state(session) == (False, ["one_click"])


def test_a_repeated_post_records_nothing_more(
    client: TestClient, session: Session, token: str
) -> None:
    client.post(f"/email/unsubscribe/{token}")
    client.post(f"/email/unsubscribe/{token}")
    assert _state(session) == (False, ["one_click"])


def test_an_unknown_token_gets_the_same_answer_and_changes_nothing(
    client: TestClient, session: Session, token: str
) -> None:
    unknown = client.post(f"/email/unsubscribe/{secrets.token_urlsafe(32)}")
    assert _state(session) == (True, [])
    known = client.post(f"/email/unsubscribe/{token}")
    assert (unknown.status_code, unknown.text) == (known.status_code, known.text)
    assert client.get(f"/email/unsubscribe/{secrets.token_urlsafe(32)}").status_code == 200


@pytest.mark.parametrize(
    "bad",
    ["..%5C..%5Cyou%5Cdelete", "x" * 20_000, "a" * 42, "a" * 44, "a" * 42 + "."],
    ids=["path tricks", "very long", "short", "long", "other character"],
)
def test_a_token_of_another_shape_gets_404_and_no_form(
    client: TestClient, session: Session, token: str, bad: str
) -> None:
    # The page never writes a form whose address someone else chose.
    shown = client.get(f"/email/unsubscribe/{bad}")
    assert shown.status_code == 404
    assert "<form" not in shown.text
    assert client.post(f"/email/unsubscribe/{bad}").status_code == 404
    assert _state(session) == (True, [])


def test_the_answer_holds_for_a_token_that_changed_nothing(client: TestClient) -> None:
    # A link whose email was purged changes nothing, yet gets the same answer, so it must not
    # promise that warnings are off.
    answer = client.post(f"/email/unsubscribe/{secrets.token_urlsafe(32)}")
    assert "If this link is still valid" in answer.text
    assert "Your data" in answer.text
