"""A fake GitHub for the sign-in tests, the app wired to it, and a helper that signs in.

The fake follows GitHub's documented web flow: the authorize page binds a new code to the PKCE
challenge the browser brought, a code works once, and the exchange answers
``bad_verification_code`` when the verifier does not hash to that challenge. Tokens are built at
run time from parts, so no token-shaped value is ever committed. No account is real.
"""

import base64
import dataclasses
import hashlib
import json
import re
import secrets
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.config import AuthSettings
from burnbird.auth.dependencies import (
    AuthState,
    allowed_repositories,
    csrf_token,
    installation_access,
    repository_access,
)
from burnbird.auth.service import OAUTH_COOKIE, github_oauth
from burnbird.web import create_app

type Body = dict[str, Any]

USER = 7001
ORG, OTHER_ORG = 3001, 3002
INSTALLATION = {ORG: 4001, OTHER_ORG: 4002}


def s256(verifier: str) -> str:
    """RFC 7636's S256 challenge, computed here independently of Authlib."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def installation_item(installation_id: int, account_id: int, kind: str = "Organization") -> Body:
    """One entry of ``GET /user/installations``, with fields Burnbird ignores."""
    return {
        "id": installation_id,
        "account": {"login": f"example-{account_id}", "id": account_id, "type": kind},
        "repository_selection": "selected",
        "app_id": 1,
        "target_type": kind,
        "permissions": {"actions": "read", "metadata": "read"},
        "events": ["workflow_job"],
        "created_at": "2026-11-13T09:00:00Z",
        "suspended_at": None,
    }


def repository_item(repository_id: int, *, private: bool = True) -> Body:
    """One entry of ``GET /user/installations/{id}/repositories``."""
    return {
        "id": repository_id,
        "name": f"example-app-{repository_id}",
        "full_name": f"example-org/example-app-{repository_id}",
        "private": private,
        "permissions": {"pull": True},
    }


class FakeGithub:
    """github.com's authorize and token endpoints and the three API calls sign-in makes."""

    def __init__(self, settings: AuthSettings) -> None:
        self.settings = settings
        self.requests: list[httpx2.Request] = []
        self.challenges: dict[str, str] = {}
        self.used: set[str] = set()
        self.token = "gh" + "u_" + secrets.token_urlsafe(30)
        self.refresh_token = "gh" + "r_" + secrets.token_urlsafe(60)
        self.user: Body = {"id": USER, "login": "example-person", "type": "User"}
        self.installations: list[Body] = []
        self.repositories: dict[int, list[Body]] = {}
        # Changes that make the fake misbehave.
        self.page_size = 100
        self.failures: dict[str, int] = {}
        self.timeouts: set[str] = set()
        self.link_host = "api.github.com"
        self.client_secret = settings.client_secret
        self.transport = httpx2.MockTransport(self.handle)

    def authorize(self, location: str) -> tuple[str, str]:
        """Do what github.com's authorize page does: bind a new code to the challenge."""
        query = parse_qs(urlsplit(location).query)
        code = secrets.token_hex(10)
        self.challenges[code] = query["code_challenge"][0]
        return code, query["state"][0]

    def exchanges(self) -> list[httpx2.Request]:
        """Every request made to the token endpoint."""
        return [r for r in self.requests if r.url.path == "/login/oauth/access_token"]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.timeouts:
            raise httpx2.ReadTimeout("timed out", request=request)
        if request.url.host == "github.com" and path == "/login/oauth/access_token":
            return self._exchange(request)
        if request.url.host == "api.github.com":
            return self._api(request, path)
        return httpx2.Response(404)

    def _exchange(self, request: httpx2.Request) -> httpx2.Response:
        if path_status := self.failures.get(request.url.path):
            return httpx2.Response(path_status, text="unavailable")
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if (form.get("client_id"), form.get("client_secret")) != (
            self.settings.client_id,
            self.client_secret,
        ):
            return httpx2.Response(200, json={"error": "incorrect_client_credentials"})
        code = form.get("code", "")
        challenge = self.challenges.get(code)
        if (
            challenge is None
            or code in self.used
            or s256(form.get("code_verifier", "")) != challenge
            or form.get("redirect_uri") != self.settings.callback_url
        ):
            return httpx2.Response(200, json={"error": "bad_verification_code"})
        self.used.add(code)
        return httpx2.Response(
            200,
            json={
                "access_token": self.token,
                "expires_in": 28800,
                "refresh_token": self.refresh_token,
                "refresh_token_expires_in": 15897600,
                "scope": "",
                "token_type": "bearer",
            },
        )

    def _api(self, request: httpx2.Request, path: str) -> httpx2.Response:
        if path_status := self.failures.get(path):
            return httpx2.Response(path_status, json={"message": "Server Error"})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx2.Response(401, json={"message": "Bad credentials"})
        if path == "/user":
            return httpx2.Response(200, json=self.user)
        if path == "/user/installations":
            return self._page(request, "installations", self.installations)
        if match := re.fullmatch(r"/user/installations/(\d+)/repositories", path):
            return self._page(request, "repositories", self.repositories.get(int(match[1]), []))
        return httpx2.Response(404)

    def _page(self, request: httpx2.Request, key: str, items: list[Body]) -> httpx2.Response:
        size = min(int(request.url.params.get("per_page", "30")), self.page_size)
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page * size < len(items):
            following = (
                f"https://{self.link_host}{request.url.path}?per_page={size}&page={page + 1}"
            )
            headers["link"] = f'<{following}>; rel="next"'
        return httpx2.Response(
            200,
            json={"total_count": len(items), key: items[(page - 1) * size : page * size]},
            headers=headers,
        )


@dataclasses.dataclass
class Clock:
    """The app's clock; real time unless a test sets ``fixed``."""

    fixed: datetime | None = None

    def __call__(self) -> datetime:
        return self.fixed or datetime.now(UTC)


@pytest.fixture
def github(auth_settings: AuthSettings) -> FakeGithub:
    return FakeGithub(auth_settings)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app(
    sessions: sessionmaker[Session], auth_settings: AuthSettings, github: FakeGithub, clock: Clock
) -> FastAPI:
    app = create_app(secrets.token_bytes(32), sessions, auth_settings)
    state = app.state.auth
    assert isinstance(state, AuthState)
    app.state.auth = dataclasses.replace(
        state, github=github_oauth(auth_settings, transport=github.transport), now=clock
    )

    # Test-only routes: the smallest pages that use each filter.
    @app.get("/_test/installations/{installation_id}")
    def _installation(installation_id: Annotated[int, Depends(installation_access)]) -> int:
        return installation_id

    @app.get("/_test/repositories/{repository_id}")
    def _repository(repository_id: Annotated[int, Depends(repository_access)]) -> int:
        return repository_id

    @app.get("/_test/repositories")
    def _repositories(
        allowed: Annotated[frozenset[int], Depends(allowed_repositories)],
    ) -> list[int]:
        return sorted(allowed)

    @app.get("/_test/csrf")
    def _csrf(token: Annotated[str, Depends(csrf_token)]) -> str:
        return token

    return app


@pytest.fixture
def client(app: FastAPI, auth_settings: AuthSettings) -> Iterator[TestClient]:
    with TestClient(app, base_url=auth_settings.origin, follow_redirects=False) as client:
        yield client


def oauth_data(client: TestClient) -> Body:
    """The decoded OAuth cookie: Starlette's signed session, ``base64(json).time.signature``."""
    value = client.cookies[OAUTH_COOKIE]
    data: Body = json.loads(base64.b64decode(value.rsplit(".", 2)[0]))
    return data


@pytest.fixture
def sign_in(client: TestClient, github: FakeGithub) -> Callable[[], httpx2.Response]:
    """Sign in through the whole flow, as a browser would; return the callback's answer."""

    def run() -> httpx2.Response:
        login = client.get("/auth/login")
        code, state = github.authorize(login.headers["location"])
        return client.get("/auth/callback", params={"code": code, "state": state})

    return run
