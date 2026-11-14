"""Sign-in's work: Authlib's GitHub client, the user's lists from GitHub, and storing a sign-in.

Authlib makes and checks ``state`` and the PKCE S256 verifier, keeps them in the signed OAuth
cookie, exchanges the code and sends the user access token on our three GET calls. The token stays
in this request's memory: it is never stored, logged or put in an exception. GitHub's web flow:
https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app
"""

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import httpx2

# The one place Authlib is imported; OAuthError is re-exported for the router.
from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]  # Authlib ships no type information
from authlib.integrations.starlette_client import OAuthError as OAuthError
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.config import AuthSettings
from burnbird.github import schemas
from burnbird.github.client import API_HEADERS, API_HOST, PER_PAGE, next_link
from burnbird.github.client import GithubAnswerError as GithubAnswerError
from burnbird.github.events import store_installation
from burnbird.models import GithubUser, Repository, UserInstallation, UserRepository, UserSession

AUTHORIZE_URL: Final = "https://github.com/login/oauth/authorize"
# Where the code is exchanged for the user access token.
EXCHANGE_URL: Final = "https://github.com/login/oauth/access_token"
# Every GitHub call has a time limit; none is retried here (the code is single use, and a failed
# sign-in is simply started again).
TIMEOUT_SECONDS: Final = 10.0
# More pages than this means something is wrong: the sign-in fails rather than stores a part.
MAX_PAGES: Final = 50

SESSION_COOKIE: Final = "__Host-burnbird_session"
SESSION_LIFETIME: Final = timedelta(hours=8)
OAUTH_COOKIE: Final = "__Host-burnbird_oauth"
# GitHub's code lives 10 minutes, so the state and verifier need no longer.
OAUTH_COOKIE_SECONDS: Final = 600


@dataclass(frozen=True, slots=True)
class SignIn:
    """What GitHub says about the person signing in: who they are and what they can access."""

    user_id: int
    login: str
    installations: tuple[schemas.Installation, ...]
    # Each repository with the installation it was listed under.
    repositories: tuple[tuple[int, schemas.Repository], ...]
    # Installations in GitHub's list that Burnbird does not handle (an enterprise account).
    skipped: int


class _User(schemas.Payload):
    id: int
    login: str


class _Installations(schemas.Payload):
    installations: tuple[dict[str, Any], ...]


class _Repositories(schemas.Payload):
    repositories: tuple[schemas.Repository, ...]


def github_oauth(
    settings: AuthSettings, transport: httpx2.AsyncBaseTransport | None = None
) -> Any:
    """Register GitHub with Authlib's Starlette client; tests pass a mock ``transport``."""
    client_kwargs: dict[str, Any] = {
        "code_challenge_method": "S256",
        "token_endpoint_auth_method": "client_secret_post",
        "timeout": TIMEOUT_SECONDS,
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    oauth = OAuth()
    return oauth.register(
        "github",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        authorize_url=AUTHORIZE_URL,
        access_token_url=EXCHANGE_URL,
        api_base_url=f"https://{API_HOST}/",
        client_kwargs=client_kwargs,
    )


async def fetch_sign_in(github: Any, token: dict[str, Any]) -> SignIn:
    """Ask GitHub who signed in, their installations of the App and their repositories in each."""
    user = _User.model_validate(await _get_json(github, token, "user"))
    installations: list[schemas.Installation] = []
    skipped = 0
    for page in await _pages(github, token, "user/installations"):
        for item in _Installations.model_validate(page).installations:
            try:
                installations.append(schemas.Installation.model_validate(item))
            except ValidationError:
                skipped += 1
    repositories = [
        (installation.id, repository)
        for installation in installations
        for page in await _pages(
            github, token, f"user/installations/{installation.id}/repositories"
        )
        for repository in _Repositories.model_validate(page).repositories
    ]
    return SignIn(user.id, user.login, tuple(installations), tuple(repositories), skipped)


async def _get_json(github: Any, token: dict[str, Any], path: str) -> Any:
    response: httpx2.Response = await github.get(path, token=token, headers=dict(API_HEADERS))
    if response.status_code != httpx2.codes.OK:
        raise GithubAnswerError(f"GET {path} answered {response.status_code}")
    return response.json()


async def _pages(github: Any, token: dict[str, Any], path: str) -> list[Any]:
    """Every page of a list, following ``Link: rel="next"`` on api.github.com only."""
    pages: list[Any] = []
    # Never ``params=``: httpx2 would replace the query of a next link with it.
    url: str | None = f"{path}?per_page={PER_PAGE}"
    while url is not None:
        if len(pages) == MAX_PAGES:
            raise GithubAnswerError(f"GET {path} has more than {MAX_PAGES} pages")
        response: httpx2.Response = await github.get(url, token=token, headers=dict(API_HEADERS))
        if response.status_code != httpx2.codes.OK:
            raise GithubAnswerError(f"GET {path} answered {response.status_code}")
        pages.append(response.json())
        url = next_link(response, f"GET {path}")
    return pages


def session_digest(session_id: str) -> bytes:
    """Return the SHA-256 of a session ID: the only form in which a session is stored."""
    return hashlib.sha256(session_id.encode()).digest()


def store_sign_in(
    sessions: sessionmaker[Session], sign_in: SignIn, now: datetime, old_session_id: str | None
) -> str:
    """Store a sign-in in one transaction and return the new session ID for the cookie.

    The user's installation and repository lists are replaced by GitHub's, installations and
    repositories not stored yet are added, and the session the request came with (if any) is
    replaced by a new one. A failure stores nothing.
    """
    session_id = secrets.token_urlsafe(32)
    user_id = sign_in.user_id
    with sessions.begin() as session:
        session.execute(
            insert(GithubUser)
            .values(id=user_id, login=sign_in.login, last_sign_in_at=now)
            .on_conflict_do_update(
                index_elements=[GithubUser.id],
                set_={"login": sign_in.login, "last_sign_in_at": now},
            )
        )
        for installation in sign_in.installations:
            store_installation(session, installation)
        for installation_id, repository in sign_in.repositories:
            values = {
                "installation_id": installation_id,
                "name": repository.name,
                "private": repository.private,
            }
            session.execute(
                insert(Repository)
                .values(id=repository.id, **values)
                .on_conflict_do_update(index_elements=[Repository.id], set_=values)
            )
        session.execute(delete(UserInstallation).where(UserInstallation.user_id == user_id))
        session.execute(delete(UserRepository).where(UserRepository.user_id == user_id))
        _insert(
            session,
            UserInstallation,
            [{"user_id": user_id, "installation_id": i.id} for i in sign_in.installations],
        )
        _insert(
            session,
            UserRepository,
            [{"user_id": user_id, "repository_id": r.id} for _, r in sign_in.repositories],
        )
        if old_session_id is not None:
            session.execute(
                delete(UserSession).where(UserSession.id_sha256 == session_digest(old_session_id))
            )
        session.add(
            UserSession(
                id_sha256=session_digest(session_id),
                user_id=user_id,
                created_at=now,
                expires_at=now + SESSION_LIFETIME,
            )
        )
    return session_id


def _insert(
    session: Session,
    table: type[UserInstallation] | type[UserRepository],
    rows: Sequence[dict[str, int]],
) -> None:
    # One repository can be listed twice only if GitHub repeats it; the key keeps one row.
    if rows:
        session.execute(insert(table).values(list(rows)).on_conflict_do_nothing())
