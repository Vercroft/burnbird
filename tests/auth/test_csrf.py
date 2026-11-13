import hashlib
from collections.abc import Callable

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from burnbird.auth.service import SESSION_COOKIE
from burnbird.models import UserSession
from tests.auth.conftest import INSTALLATION, ORG, FakeGithub, installation_item, repository_item

type SignIn = Callable[[], httpx2.Response]

ORIGIN = "https://burnbird.test"


@pytest.fixture
def signed_in(client: TestClient, github: FakeGithub, sign_in: SignIn) -> str:
    """Sign in and return the session's CSRF token."""
    github.installations = [installation_item(INSTALLATION[ORG], ORG)]
    github.repositories = {INSTALLATION[ORG]: [repository_item(5001)]}
    assert sign_in().status_code == 303
    token = client.get("/_test/csrf").json()
    assert isinstance(token, str)
    return token


def _session_ids(session: Session) -> set[bytes]:
    session.expire_all()
    return set(session.scalars(select(UserSession.id_sha256)))


def test_sign_out_deletes_only_its_own_session(
    client: TestClient, app: FastAPI, github: FakeGithub, signed_in: str, session: Session
) -> None:
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as other:
        login = other.get("/auth/login")
        code, state = github.authorize(login.headers["location"])
        assert (
            other.get("/auth/callback", params={"code": code, "state": state}).status_code == 303
        )
        theirs = hashlib.sha256(other.cookies[SESSION_COOKIE].encode()).digest()
    assert len(_session_ids(session)) == 2
    response = client.post(
        "/auth/logout", data={"csrf_token": signed_in}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert _session_ids(session) == {theirs}
    assert client.get("/_test/repositories").status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://burnbird.test",
        "https://burnbird.test:8443",
        "https://BURNBIRD.test",
        "https://burnbird.test.example.com",
        "https://example.com",
        "https://burnbird.test/",
    ],
)
def test_a_post_from_anywhere_else_is_refused(
    origin: str | None, client: TestClient, signed_in: str, session: Session
) -> None:
    before = _session_ids(session)
    headers = {} if origin is None else {"Origin": origin}
    response = client.post("/auth/logout", data={"csrf_token": signed_in}, headers=headers)
    assert response.status_code == 403
    assert _session_ids(session) == before


@pytest.mark.parametrize(
    "form",
    [{}, {"csrf_token": ""}, {"csrf_token": "x" * 43}, {"other": "x"}],
)
def test_a_post_without_the_sessions_token_is_refused(
    form: dict[str, str], client: TestClient, signed_in: str, session: Session
) -> None:
    before = _session_ids(session)
    response = client.post("/auth/logout", data=form, headers={"Origin": ORIGIN})
    assert response.status_code == 403
    assert _session_ids(session) == before


def test_a_token_sent_twice_is_refused(
    client: TestClient, signed_in: str, session: Session
) -> None:
    response = client.post(
        "/auth/logout",
        content=f"csrf_token={signed_in}&csrf_token={signed_in}",
        headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 403


def test_another_sessions_token_is_refused(
    client: TestClient, app: FastAPI, github: FakeGithub, signed_in: str, session: Session
) -> None:
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as other:
        login = other.get("/auth/login")
        code, state = github.authorize(login.headers["location"])
        other.get("/auth/callback", params={"code": code, "state": state})
        theirs = other.get("/_test/csrf").json()
    assert theirs != signed_in
    response = client.post("/auth/logout", data={"csrf_token": theirs}, headers={"Origin": ORIGIN})
    assert response.status_code == 403


def test_a_post_without_a_session_is_refused(client: TestClient) -> None:
    response = client.post("/auth/logout", data={"csrf_token": "x"}, headers={"Origin": ORIGIN})
    assert response.status_code == 403


def test_the_token_is_tied_to_the_session_and_reveals_nothing_of_it(
    client: TestClient, signed_in: str
) -> None:
    cookie = client.cookies[SESSION_COOKIE]
    assert cookie not in signed_in
    assert signed_in != hashlib.sha256(cookie.encode()).hexdigest()
    assert client.get("/_test/csrf").json() == signed_in
