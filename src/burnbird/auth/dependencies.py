"""Who is signed in, what they may see, and the CSRF check every POST runs.

Two filters, one rule. GitHub lists an installation for a user who can access any one of its
repositories, so anything that names a repository, workflow, runner or label is filtered by
``allowed_repositories``; installation-wide numbers without names use ``allowed_installations``.
A foreign ID answers 404, never 403, so a page does not reveal that it exists.
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
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from burnbird.auth.config import AuthSettings
from burnbird.auth.service import SESSION_COOKIE, session_digest
from burnbird.models import UserInstallation, UserRepository, UserSession

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
