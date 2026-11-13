"""``/auth/login``, ``/auth/callback`` and ``/auth/logout``: sign-in with GitHub and sign-out.

Login and callback are ``async def`` because Authlib's Starlette client is; the callback's database
work runs in the thread pool. The callback exchanges the code only after Authlib has matched
``state`` against the signed OAuth cookie, and never retries the exchange: a code works once.
Every answer it gives clears the OAuth cookie and is never cached. Nothing from the query string
is logged except an error code of GitHub's documented shape.
"""

import logging
import re
from typing import Final

import httpx2
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from burnbird.auth.dependencies import auth_state, csrf_protected
from burnbird.auth.service import (
    SESSION_COOKIE,
    SESSION_LIFETIME,
    GithubAnswerError,
    OAuthError,
    fetch_sign_in,
    session_digest,
    store_sign_in,
)
from burnbird.models import UserSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

# Exchange errors that mean our own configuration is wrong, not the visitor's sign-in:
# https://docs.github.com/en/apps/oauth-apps/maintaining-oauth-apps/troubleshooting-oauth-app-access-token-request-errors
CONFIGURATION_ERRORS: Final = frozenset({"incorrect_client_credentials", "redirect_uri_mismatch"})
_ERROR_CODE: Final = re.compile(r"[a-z_]{1,40}")
# Where a signed-in visitor goes; the dashboard (a later change) serves it.
HOME: Final = "/"


@router.get("/login")
async def login(request: Request) -> Response:
    """Send the visitor to GitHub with a new ``state`` and PKCE S256 challenge."""
    state = auth_state(request)
    response: Response = await state.github.authorize_redirect(
        request, redirect_uri=state.settings.callback_url
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/callback")
async def callback(request: Request) -> Response:
    """Finish sign-in: check state, exchange the code, store what GitHub lists, start a session."""
    state = auth_state(request)
    query = request.query_params
    try:
        if "error" in query:
            logger.warning("sign-in: GitHub returned %s", _error_code(query["error"]))
            return _text(status.HTTP_400_BAD_REQUEST, "Sign-in did not finish. Sign in again.")
        if "state" not in query:
            # GitHub's own redirect after an install starts the flow with no state or PKCE, so its
            # code is never exchanged; a new flow with both starts instead.
            logger.info("sign-in: callback without state, restarted")
            return RedirectResponse("/auth/login", status.HTTP_303_SEE_OTHER)
        try:
            token = await state.github.authorize_access_token(request)
        except (httpx2.HTTPError, ValueError) as err:
            logger.warning("sign-in: the code exchange failed: %s", type(err).__name__)
            return _text(status.HTTP_502_BAD_GATEWAY, "GitHub did not answer. Sign in again.")
        except OAuthError as err:
            # A state mismatch (``mismatching_state``) or an error from GitHub's token endpoint.
            code = str(err.error)
            if code in CONFIGURATION_ERRORS:
                logger.error("sign-in: GitHub refused our configuration: %s", code)
                return _text(status.HTTP_500_INTERNAL_SERVER_ERROR, "Sign-in is not available.")
            logger.warning("sign-in: refused: %s", _error_code(code))
            return _text(status.HTTP_400_BAD_REQUEST, "Sign-in expired. Sign in again.")
        if not isinstance(token, dict) or not isinstance(token.get("access_token"), str):
            logger.warning("sign-in: the token answer has no access token")
            return _text(status.HTTP_502_BAD_GATEWAY, "GitHub did not answer. Sign in again.")
        try:
            sign_in = await fetch_sign_in(state.github, token)
        except (
            httpx2.HTTPError,
            GithubAnswerError,
            OAuthError,
            ValidationError,
            ValueError,
        ) as err:
            logger.warning("sign-in: reading the user's lists failed: %s", type(err).__name__)
            return _text(status.HTTP_502_BAD_GATEWAY, "GitHub did not answer. Sign in again.")
        try:
            session_id = await run_in_threadpool(
                store_sign_in,
                state.sessions,
                sign_in,
                state.now(),
                request.cookies.get(SESSION_COOKIE),
            )
        except SQLAlchemyError as err:
            # Answered here, not left to the server's error page, so the OAuth cookie is cleared.
            logger.error("sign-in: storing the sign-in failed: %s", type(err).__name__)
            return _text(status.HTTP_500_INTERNAL_SERVER_ERROR, "Sign-in failed. Sign in again.")
        logger.info(
            "sign-in: user %d, %d installations (%d skipped), %d repositories",
            sign_in.user_id,
            len(sign_in.installations),
            sign_in.skipped,
            len(sign_in.repositories),
        )
        response: Response = RedirectResponse(
            HOME, status.HTTP_303_SEE_OTHER, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=int(SESSION_LIFETIME.total_seconds()),
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response
    finally:
        # Only Authlib's state lives in this session; any outcome clears it, so the browser drops
        # the OAuth cookie and a state is never used twice.
        request.session.clear()


@router.post("/logout", dependencies=[Depends(csrf_protected)])
def logout(request: Request) -> Response:
    """End the session: delete its row and the cookie."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        with auth_state(request).sessions.begin() as session:
            session.execute(
                delete(UserSession).where(UserSession.id_sha256 == session_digest(session_id))
            )
    response = RedirectResponse(HOME, status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


def _error_code(code: str) -> str:
    return code if _ERROR_CODE.fullmatch(code) else "other"


def _text(status_code: int, message: str) -> Response:
    return PlainTextResponse(message, status_code, headers={"Cache-Control": "no-store"})
