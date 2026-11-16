"""Every answer carries the security headers: a page, a redirect, and errors."""

import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.config import AuthSettings
from burnbird.web import create_app

EXPECTED = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; "
        "base-uri 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


@pytest.fixture
def client(sessions: sessionmaker[Session], auth_settings: AuthSettings) -> TestClient:
    app = create_app(secrets.token_bytes(32), sessions, auth_settings)
    return TestClient(app, base_url=auth_settings.origin, follow_redirects=False)


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/you/deleted", 200),
        ("GET", "/you", 303),
        ("GET", "/auth/callback", 303),
        ("GET", "/no-such-page", 404),
        ("POST", "/you/delete", 403),
        ("POST", "/auth/logout", 403),
        ("POST", "/github/webhook", 403),
    ],
    ids=[
        "page",
        "redirect to sign in",
        "sign-in redirect",
        "not found",
        "refused delete",
        "refused sign-out",
        "unsigned webhook",
    ],
)
def test_every_answer_carries_the_headers_once(
    client: TestClient, method: str, path: str, status: int
) -> None:
    response = client.request(method, path)
    assert response.status_code == status
    for name, value in EXPECTED.items():
        assert response.headers.get_list(name) == [value], name


def test_a_body_over_the_limit_carries_the_headers_too(client: TestClient) -> None:
    response = client.post("/github/webhook", content=b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert response.headers["content-security-policy"] == EXPECTED["content-security-policy"]
