"""Every POST route depends on ``csrf_protected``, except the ones listed here with their reason.

FastAPI 0.162 keeps an included router as one entry of ``app.routes`` whose ``original_router``
holds its routes, so the walk below opens those; any other kind of entry fails the test, so a
route cannot hide from it.
"""

import secrets
from collections.abc import Iterator

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session, sessionmaker
from starlette.routing import BaseRoute

from burnbird.auth.config import AuthSettings
from burnbird.auth.dependencies import csrf_protected
from burnbird.web import create_app

EXEMPT = {
    # GitHub's webhook proves itself with its HMAC signature, and carries no session.
    "/github/webhook",
    # RFC 8058's one-click POST comes from the mail client with no cookie or other context: the
    # token in the path is its only proof, and it can only turn a kind of email off.
    "/email/unsubscribe/{token}",
    # The email provider's webhook proves itself with its HMAC over the raw body, and carries no
    # session.
    "/email/webhook",
}


def _api_routes(routes: list[BaseRoute]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        included = getattr(route, "original_router", None)
        assert included is not None, f"an entry the walk cannot open: {type(route).__name__}"
        yield from _api_routes(included.routes)


def _calls(dependant: Dependant) -> Iterator[object]:
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _calls(dependency)


def test_every_post_but_the_exempt_ones_checks_csrf(
    sessions: sessionmaker[Session], auth_settings: AuthSettings
) -> None:
    # With the email webhook's key, so its route is there to be checked.
    app = create_app(
        secrets.token_bytes(32), sessions, auth_settings, email_webhook_key=secrets.token_bytes(32)
    )
    posts = {
        route.path: route
        for route in _api_routes(app.routes)
        if "POST" in (route.methods or set())
    }
    unprotected = {
        path for path, route in posts.items() if csrf_protected not in _calls(route.dependant)
    }
    assert unprotected == EXEMPT
    assert {"/auth/logout", "/you/email", "/you/delete"} <= set(posts)
