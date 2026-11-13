"""The web command's app: it creates the FastAPI app and includes each feature's router."""

from datetime import UTC, datetime
from typing import Final

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import Secret
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.sessions import SessionMiddleware

from burnbird import logs
from burnbird.auth import router as auth
from burnbird.auth.config import AuthSettings, settings_from_environment
from burnbird.auth.dependencies import AuthState
from burnbird.auth.service import OAUTH_COOKIE, OAUTH_COOKIE_SECONDS, github_oauth
from burnbird.database import database_url, make_engine
from burnbird.github import config
from burnbird.github import router as github
from burnbird.github.router import WebhookState

# The largest request body accepted: about 100 job events, or an installation event that lists
# about 7,400 repositories. GitHub caps a payload at 25 MB. A larger body gets 413, refused on its
# Content-Length or as soon as the bytes read pass the limit:
# https://github.com/Kludex/starlette/blob/1.10.0/docs/middleware.md
MAX_BODY_BYTES: Final = 1024 * 1024


def create_app(
    webhook_secret: bytes, sessions: sessionmaker[Session], auth_settings: AuthSettings
) -> FastAPI:
    """Create the app with its body limit, the OAuth state cookie and every router."""
    # No generated API pages: the site serves only its own pages and loads nothing from elsewhere.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY_BYTES)
    # Holds only Authlib's state and PKCE verifier between /auth/login and /auth/callback: a
    # signed __Host- cookie whose age the server checks. The sign-in session is our own cookie.
    app.add_middleware(
        SessionMiddleware,
        secret_key=Secret(auth_settings.state_key),
        session_cookie=OAUTH_COOKIE,
        max_age=OAUTH_COOKIE_SECONDS,
        path="/",
        same_site="lax",
        https_only=True,
    )
    app.state.webhook = WebhookState(webhook_secret, sessions)
    app.state.auth = AuthState(
        auth_settings, sessions, github_oauth(auth_settings), lambda: datetime.now(UTC)
    )
    app.include_router(github.router)
    app.include_router(auth.router)
    return app


def app_from_environment() -> FastAPI:
    """Create the app for the server: secrets from their files, the database from its URL."""
    logs.configure()
    return create_app(
        config.webhook_secret(),
        sessionmaker(make_engine(database_url())),
        settings_from_environment(),
    )
