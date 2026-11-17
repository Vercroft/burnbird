"""Sign-in's work: Authlib's GitHub client, the user's lists from GitHub, and storing a sign-in.

Authlib makes and checks ``state`` and the PKCE S256 verifier, keeps them in the signed OAuth
cookie, exchanges the code and sends the user access token on our GET calls. The token stays in
this request's memory: it is never stored, logged or put in an exception. GitHub's web flow:
https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app

Each sign-in also reads the user's email addresses, and only the one GitHub marks both primary and
verified is kept. ``GET /user/emails`` needs the App's "Email addresses" user permission; without
it, "the API will return a 403 response"
(https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app).
A 403 or 404, or no primary verified address (or one ``user_email`` could not store), deletes the
stored one. Any other failure keeps it and the sign-in goes on: the address is never needed to
sign in, and never logged.
"""

import hashlib
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx2

# The one place Authlib is imported; OAuthError is re-exported for the router.
from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]  # Authlib ships no type information
from authlib.integrations.starlette_client import OAuthError as OAuthError
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.config import AuthSettings
from burnbird.email.preferences import write_defaults
from burnbird.github import schemas
from burnbird.github.client import (
    API_HEADERS,
    API_HOST,
    PER_PAGE,
    RateLimitedError,
    next_link,
    rate_limit_resume,
)
from burnbird.github.client import GithubAnswerError as GithubAnswerError
from burnbird.github.events import store_installation, store_repository
from burnbird.models import (
    GithubUser,
    Installation,
    Repository,
    UserEmail,
    UserInstallation,
    UserRepository,
    UserSession,
    storable_address,
)
from burnbird.retention import lock_installations_of, lock_repositories

logger = logging.getLogger(__name__)

AUTHORIZE_URL: Final = "https://github.com/login/oauth/authorize"
# Where the code is exchanged for the user access token.
EXCHANGE_URL: Final = "https://github.com/login/oauth/access_token"
# GitHub's answers to ``GET /user/emails`` when the App may not read the user's addresses.
NO_EMAIL_PERMISSION: Final = frozenset({403, 404})
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
class ListedRepository:
    """A repository GitHub listed for the user, under which installation, and their admin flag."""

    installation_id: int
    repository: schemas.Repository
    # GitHub's ``permissions.admin`` for this user; anything but exactly ``true`` is False.
    admin: bool


@dataclass(frozen=True, slots=True)
class SignIn:
    """What GitHub says about the person signing in: who they are and what they can access."""

    user_id: int
    login: str
    installations: tuple[schemas.Installation, ...]
    repositories: tuple[ListedRepository, ...]
    # Installations in GitHub's list that Burnbird does not handle (an enterprise account).
    skipped: int
    # The primary verified address, or None when GitHub gave none or may not share one.
    email: str | None = None
    # False when GitHub's answer could not be read: the stored address, if any, is kept.
    email_read: bool = True


class _User(schemas.Payload):
    id: int
    login: str


class _Installations(schemas.Payload):
    installations: tuple[dict[str, Any], ...]


class _Repositories(schemas.Payload):
    # Items stay raw here, so each one's ``permissions`` can be read strictly (see ``_admin``).
    repositories: tuple[dict[str, Any], ...]


class _Email(schemas.Payload):
    email: str
    primary: bool
    verified: bool


_EMAILS: Final = TypeAdapter(list[_Email])


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
        ListedRepository(installation.id, schemas.Repository.model_validate(item), _admin(item))
        for installation in installations
        for page in await _pages(
            github, token, f"user/installations/{installation.id}/repositories"
        )
        for item in _Repositories.model_validate(page).repositories
    ]
    email, email_read = await _primary_email(github, token)
    return SignIn(
        user.id, user.login, tuple(installations), tuple(repositories), skipped, email, email_read
    )


async def _primary_email(github: Any, token: dict[str, Any]) -> tuple[str | None, bool]:
    """Return the primary verified address and whether GitHub's answer was read.

    A 403 or 404 (not a rate limit) means the App may not read the addresses: none is kept. A
    rate limit, a network error or an answer not as documented keeps the stored one.
    """
    try:
        pages = await _pages(github, token, "user/emails")
        emails = [email for page in pages for email in _EMAILS.validate_python(page)]
    except RateLimitedError:
        logger.warning("sign-in: the email addresses were rate limited; the stored one is kept")
        return None, False
    except GithubAnswerError as err:
        if err.status in NO_EMAIL_PERMISSION:
            logger.info("sign-in: GitHub shares no email address (%d)", err.status)
            return None, True
        logger.warning("sign-in: reading the email addresses failed; the stored one is kept")
        return None, False
    except (httpx2.HTTPError, OAuthError, ValidationError, ValueError) as err:
        logger.warning(
            "sign-in: reading the email addresses failed: %s; the stored one is kept",
            type(err).__name__,
        )
        return None, False
    primary = next((e.email for e in emails if e.primary and e.verified), None)
    if primary is not None and not storable_address(primary):
        # The table would refuse it, and the whole sign-in with it: kept as no address at all.
        logger.warning("sign-in: GitHub's primary address is not usable; none is stored")
        return None, True
    return primary, True


def _admin(item: dict[str, Any]) -> bool:
    """Return whether GitHub says the user has admin on the repository: exactly ``true`` only.

    "The access the user has to each repository is included in the hash under the permissions
    key" (https://docs.github.com/en/rest/apps/installations). A missing key, a missing object or
    any value but the JSON boolean ``true`` gives no admin, so a malformed answer never grants
    account-wide access.
    """
    permissions = item.get("permissions")
    return isinstance(permissions, dict) and permissions.get("admin") is True


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
            if rate_limit_resume(response, datetime.now(UTC)) is not None:
                raise RateLimitedError(f"GET {path}", datetime.now(UTC))
            raise GithubAnswerError(
                f"GET {path} answered {response.status_code}", response.status_code
            )
        pages.append(response.json())
        url = next_link(response, f"GET {path}")
    return pages


def session_digest(session_id: str) -> bytes:
    """Return the SHA-256 of a session ID: the only form in which a session is stored."""
    return hashlib.sha256(session_id.encode()).digest()


def store_sign_in(
    sessions: sessionmaker[Session],
    sign_in: SignIn,
    now: datetime,
    old_session_id: str | None,
    device: str | None = None,
) -> str:
    """Store a sign-in in one transaction and return the new session ID for the cookie.

    The user's installation and repository lists are replaced by GitHub's, installations and
    repositories not stored yet are added, and the session the request came with (if any) is
    replaced by a new one, labelled with ``device``. The stored email address is replaced by the
    one GitHub gave, or deleted when it gave none; it is kept as it was when GitHub's answer could
    not be read. A stored address writes the default email choices the user has none for yet.
    A failure stores nothing.
    """
    session_id = secrets.token_urlsafe(32)
    user_id = sign_in.user_id
    repositories = sorted(sign_in.repositories, key=lambda listed: listed.repository.id)
    with sessions.begin() as session:
        # In the lock order (``lock_repositories``): first every listed account's installation
        # lock, so no other path stores an installation for one of them until this commits, and
        # the stale installations read next stay as read. Then, as Delete my data takes them, the
        # stored repositories, then the accounts, then the user row, then the user's own rows.
        # FOR NO KEY UPDATE is the lock the repository upsert takes anyway, so it holds nothing up
        # that the upsert would not: a re-queue's FOR KEY SHARE goes on, Delete my data's FOR
        # UPDATE waits.
        # The repositories of a stale installation (``store_installation`` uninstalls it) are
        # locked here too, before any account, and then FOR UPDATE, the lock its deletes take:
        # upgrading later would wait for a FOR KEY SHARE taken meanwhile, whose path may wait for
        # this one.
        lock_installations_of(session, [i.account.id for i in sign_in.installations])
        stale = session.scalars(
            select(Installation.id).where(
                Installation.account_id.in_([i.account.id for i in sign_in.installations]),
                Installation.id.not_in([i.id for i in sign_in.installations]),
            )
        ).all()
        locked = select(Repository.id).where(
            or_(
                Repository.id.in_([r.repository.id for r in repositories]),
                Repository.installation_id.in_(stale),
            )
        )
        lock_repositories(session, locked, lock="UPDATE" if stale else "NO KEY UPDATE")
        for installation in sorted(sign_in.installations, key=lambda i: i.account.id):
            store_installation(session, installation)
        for listed in repositories:
            store_repository(session, listed.installation_id, listed.repository)
        session.execute(
            insert(GithubUser)
            .values(id=user_id, login=sign_in.login, last_sign_in_at=now)
            .on_conflict_do_update(
                index_elements=[GithubUser.id],
                set_={"login": sign_in.login, "last_sign_in_at": now},
            )
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
            [
                {"user_id": user_id, "repository_id": r.repository.id, "admin": r.admin}
                for r in sign_in.repositories
            ],
        )
        if sign_in.email_read:
            _store_email(session, user_id, sign_in.email, now)
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
                device=device,
            )
        )
    return session_id


def _store_email(session: Session, user_id: int, address: str | None, now: datetime) -> None:
    if address is None:
        session.execute(delete(UserEmail).where(UserEmail.user_id == user_id))
        return
    session.execute(
        insert(UserEmail)
        .values(user_id=user_id, address=address, read_at=now)
        .on_conflict_do_update(
            index_elements=[UserEmail.user_id], set_={"address": address, "read_at": now}
        )
    )
    # The first stored address brings the default email choices; later ones keep the user's.
    write_defaults(session, user_id, now)


def _insert(
    session: Session,
    table: type[UserInstallation] | type[UserRepository],
    rows: Sequence[dict[str, int]],
) -> None:
    # One repository can be listed twice only if GitHub repeats it; the key keeps one row.
    if rows:
        session.execute(insert(table).values(list(rows)).on_conflict_do_nothing())
