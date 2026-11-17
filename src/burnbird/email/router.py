"""``/email/unsubscribe/{token}``: RFC 8058 one-click unsubscribe.

GET shows a page with one button and changes nothing: link checkers, virus scanners and mail
previews open links with GET. The button, or the mail client itself, sends the POST, which turns
the email's kind off in one transaction and answers 200 with no redirect, as RFC 8058 asks. The
POST needs no session and no CSRF token: the mail client sends it with no cookie or other context,
so the token in the path is its only proof (``tests/test_csrf_boundary.py`` lists it). An unknown
or used token gets the same answer, so the page tells no one which tokens exist. A token that is
not shaped like one (43 URL-safe characters, ``secrets.token_urlsafe(32)``) answers 404, so the
page never writes a form address someone else chose and the path stays short.
"""

import re
from typing import Final

from fastapi import APIRouter, HTTPException, Request, Response, status

from burnbird.auth.dependencies import auth_state
from burnbird.email.unsubscribe import turn_off
from burnbird.templates import page

router = APIRouter(prefix="/email")

_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def _checked(token: str) -> str:
    if not _TOKEN.fullmatch(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return token


@router.get("/unsubscribe/{token}")
def confirm(token: str) -> Response:
    """Show the button that unsubscribes; nothing is written."""
    return page(
        "email/pages/unsubscribe.html", title="Stop run-out warnings", token=_checked(token)
    )


@router.post("/unsubscribe/{token}")
def unsubscribe(request: Request, token: str) -> Response:
    """Turn the email's kind off; the same answer whatever the token."""
    _checked(token)
    state = auth_state(request)
    with state.sessions.begin() as session:
        turn_off(session, token, state.now())
    return page("email/pages/unsubscribed.html", title="Unsubscribe from run-out warnings")
