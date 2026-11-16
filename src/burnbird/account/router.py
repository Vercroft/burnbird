"""``/you``: the "Your data" page, the run-out warnings switch, and Delete my data.

Signed in only: without a live session ``GET /you`` sends the visitor to sign in. Both forms are
POSTs checked by ``csrf_protected``. Delete my data runs in one transaction, then the session
cookie is cleared and the browser is sent to a plain page that needs no session.
"""

from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from burnbird import retention
from burnbird.account.service import your_data
from burnbird.auth.dependencies import (
    auth_state,
    csrf_protected,
    csrf_token,
    current_user,
    posted_form,
    require_user,
)
from burnbird.auth.service import SESSION_COOKIE
from burnbird.email import consent
from burnbird.email.preferences import set_choice
from burnbird.templates import page

router = APIRouter(prefix="/you")

PRIVACY_URL: Final = "https://github.com/Vercroft/burnbird/blob/main/PRIVACY.md"
# The form field of the run-out warnings switch; a ticked checkbox sends "on".
WARNINGS_FIELD: Final = "usage_warnings"
# The required checkbox of the Delete my data form; the server checks it too.
CONFIRM_DELETE_FIELD: Final = "confirm_delete"


@router.get("")
def show(request: Request, user: Annotated[int | None, Depends(current_user)]) -> Response:
    """Show what Burnbird stores about the signed-in user."""
    if user is None:
        return RedirectResponse("/auth/login", status.HTTP_303_SEE_OTHER)
    state = auth_state(request)
    with state.sessions.begin() as session:
        data = your_data(session, user, state.now())
    until = None if data.read_at is None else data.read_at + retention.EMAIL_FRESHNESS
    return page(
        "account/you.html",
        title="Your data",
        data=data,
        read_at=None if data.read_at is None else _moment(data.read_at),
        warnings_until=None if until is None else _day(until),
        sessions=[(s.device or "Unknown device", _moment(s.started_at)) for s in data.sessions],
        consent_text=consent.TEXT,
        warnings_field=WARNINGS_FIELD,
        csrf_token=csrf_token(request),
        privacy_url=PRIVACY_URL,
        confirm_field=CONFIRM_DELETE_FIELD,
    )


@router.post("/email", dependencies=[Depends(csrf_protected)])
def save_email_choice(
    request: Request,
    user: Annotated[int, Depends(require_user)],
    form: Annotated[dict[str, list[str]], Depends(posted_form)],
) -> Response:
    """Save the run-out warnings switch; a change is recorded once, a repeat not at all.

    A ticked box sends the field once, as "on"; an unticked one sends nothing. Anything else (a
    repeated field, another value) is not this form, so it changes nothing.
    """
    sent = form.get(WARNINGS_FIELD)
    if sent is not None and sent != ["on"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the warnings switch is not valid")
    state = auth_state(request)
    with state.sessions.begin() as session:
        set_choice(session, user, "usage_warnings", sent == ["on"], state.now())
    return RedirectResponse("/you", status.HTTP_303_SEE_OTHER)


@router.post("/delete", dependencies=[Depends(csrf_protected)])
def delete(
    request: Request,
    user: Annotated[int, Depends(require_user)],
    form: Annotated[dict[str, list[str]], Depends(posted_form)],
) -> Response:
    """Delete my data, in one transaction, then end the session in the browser too.

    It cannot be undone, so the form's confirm box must be ticked: exactly one "yes".
    """
    if form.get(CONFIRM_DELETE_FIELD) != ["yes"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tick the box to confirm")
    with auth_state(request).sessions.begin() as session:
        retention.delete_my_data(session, user)
    response = RedirectResponse("/you/deleted", status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/deleted")
def deleted() -> Response:
    """Say the data is deleted; it needs no session, since there is none left."""
    return page("account/deleted.html", title="Your data is deleted")


def _moment(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def _day(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%d %b %Y")
