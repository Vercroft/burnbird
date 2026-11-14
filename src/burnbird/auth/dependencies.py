"""Who is signed in, what they may see, and the CSRF check every POST runs.

Everything a page shows about repositories (names and numbers alike) is filtered by
``allowed_repositories``. GitHub lists an installation for anyone who can access one of its
repositories, outside collaborators included, so ``allowed_installations`` alone never grants a
view or a setting.

Account-wide numbers, the forecast and settings need ``account_access``: a personal account's own
user, or, for an organisation, a user with admin on every repository the installation covers.
Everyone else sees only their own repositories. The user's lists (``user_installation`` and
``user_repository``, admin flags included) come from their last sign-in, so they can be up to one
session (8 hours) old. The installation's repository list comes from the hourly reconciliation and
counts only if it was stored within ``SYNC_FRESHNESS``. Anything not proven grants nothing: an
installation never synced or not synced recently, one with no repository, a user it is not listed
for. A foreign ID answers 404, never 403, so a page does not reveal that it exists.
"""

import base64
import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.config import AuthSettings
from burnbird.auth.service import SESSION_COOKIE, session_digest
from burnbird.github.reconcile import INTERVAL as RECONCILE_INTERVAL
from burnbird.models import (
    Account,
    Installation,
    Repository,
    UserInstallation,
    UserRepository,
    UserSession,
)

# An organisation's repository list counts only if a reconciliation stored it this recently: one
# missed hourly run is allowed. Older, a repository GitHub added since may be missing, and that
# must not grant access.
SYNC_FRESHNESS: Final = 2 * RECONCILE_INTERVAL

# The form field every POST form carries.
CSRF_FIELD: Final = "csrf_token"
_CSRF_MESSAGE: Final = b"burnbird-csrf"


@dataclass(frozen=True, slots=True)
class AuthState:
    """What sign-in needs from the app: settings, database sessions, the GitHub client, a clock."""

    settings: AuthSettings
    sessions: sessionmaker[Session]
    # Authlib's registered GitHub client (untyped).
    github: Any
    now: Callable[[], datetime]


def auth_state(request: Request) -> AuthState:
    """Return the state ``burnbird.web.create_app`` put on the app."""
    state = request.app.state.auth
    if not isinstance(state, AuthState):
        raise TypeError("the app has no auth state")
    return state


def current_user(request: Request) -> int | None:
    """Return the signed-in user's ID, or None without a live session."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    state = auth_state(request)
    with state.sessions.begin() as session:
        return session.scalar(
            select(UserSession.user_id).where(
                UserSession.id_sha256 == session_digest(session_id),
                UserSession.expires_at > state.now(),
            )
        )


def require_user(user: Annotated[int | None, Depends(current_user)]) -> int:
    """Return the signed-in user's ID; 401 without a live session."""
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")
    return user


def allowed_installations(
    request: Request, user: Annotated[int, Depends(require_user)]
) -> frozenset[int]:
    """Return the installations the user could see at their last sign-in."""
    with auth_state(request).sessions.begin() as session:
        return frozenset(
            session.scalars(
                select(UserInstallation.installation_id).where(UserInstallation.user_id == user)
            )
        )


def allowed_repositories(
    request: Request, user: Annotated[int, Depends(require_user)]
) -> frozenset[int]:
    """Return the repositories the user could access at their last sign-in."""
    with auth_state(request).sessions.begin() as session:
        return frozenset(
            session.scalars(
                select(UserRepository.repository_id).where(UserRepository.user_id == user)
            )
        )


def account_access(session: Session, user_id: int, installation_id: int) -> bool:
    """Return whether the user may see the installation's account-wide numbers and settings.

    One query, true only when the installation is listed for the user and either:
    - the account is the user's own personal account (type ``User``, the user's own ID); or
    - the account is an organisation, a reconciliation stored its full repository list within
      ``SYNC_FRESHNESS`` (by the database's clock), it holds at least one repository, and the
      user had admin on every one of them at their last sign-in.
    Anything else, an unknown installation included, is False.
    """
    repositories = select(Repository.id).where(Repository.installation_id == Installation.id)
    lacking_admin = repositories.where(
        ~exists().where(
            UserRepository.user_id == user_id,
            UserRepository.repository_id == Repository.id,
            UserRepository.admin.is_(True),
        )
    )
    allowed = (
        select(Installation.id)
        .join(Account, Account.id == Installation.account_id)
        .join(
            UserInstallation,
            and_(
                UserInstallation.installation_id == Installation.id,
                UserInstallation.user_id == user_id,
            ),
        )
        .where(
            Installation.id == installation_id,
            or_(
                and_(Account.type == "User", Account.id == user_id),
                and_(
                    Account.type == "Organization",
                    # NULL (never synced) compares as unknown, so it never passes.
                    Installation.repositories_synced_at >= func.now() - SYNC_FRESHNESS,
                    repositories.exists(),
                    ~lacking_admin.exists(),
                ),
            ),
        )
    )
    return session.scalar(select(allowed.exists())) is True


def installation_access(
    installation_id: int, allowed: Annotated[frozenset[int], Depends(allowed_installations)]
) -> int:
    """Return ``installation_id`` when the user may see it; 404 otherwise."""
    if installation_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return installation_id


def repository_access(
    repository_id: int, allowed: Annotated[frozenset[int], Depends(allowed_repositories)]
) -> int:
    """Return ``repository_id`` when the user may access it; 404 otherwise."""
    if repository_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return repository_id


def csrf_token(request: Request) -> str:
    """Return the CSRF token for the request's session, for a form's ``csrf_token`` field.

    It is an HMAC-SHA256 keyed by the session ID, which exists only in the browser's cookie (the
    database holds its hash), so it is bound to the session and needs no stored value.
    OWASP, HMAC CSRF tokens:
    https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")
    return _token(session_id)


async def csrf_protected(request: Request) -> None:
    """Refuse a POST (403) unless it comes from the site and carries the session's token.

    The one other ``async def``: it awaits the body and makes no blocking call. The ``Origin``
    must be the site's origin exactly; a missing or ``null`` Origin is refused, as OWASP says it is
    not evidence of a same-origin request. The form is parsed with the standard library, so no
    multipart parser is needed.
    """
    if request.headers.get("origin") != auth_state(request).settings.origin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the request did not come from this site")
    session_id = request.cookies.get(SESSION_COOKIE)
    sent = parse_qs((await request.body()).decode("utf-8", "replace")).get(CSRF_FIELD, [])
    if (
        not session_id
        or len(sent) != 1
        or not hmac.compare_digest(sent[0].encode(), _token(session_id).encode())
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the form is out of date")


def _token(session_id: str) -> str:
    digest = hmac.new(session_id.encode(), _CSRF_MESSAGE, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
