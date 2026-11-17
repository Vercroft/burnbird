"""``POST /email/webhook``: the provider reports hard bounces, and the address is paused.

The order is fixed, as for GitHub's webhook (``burnbird.github.router``). The app's body limit
refuses an oversized body while it is read (413); the signature over the raw body is checked
before anything in it is read (403, ``webhook_signature``); only then is the body parsed (400 if
it is not the documented shape). Every signed request is answered 200 with an empty body, an
ignored one too, so the provider has nothing to retry. The route exists only when the key is set.

Each item names the email it is about by ``client_reference``, the outbox row's ID. An unknown
one is ignored: the user may have deleted their data. The provider names the event in
``event_data[].object``; ``hardbounce`` pauses the address when it is the user's address now,
and every other event is ignored. What a request does is safe to repeat, since the signature
does not cover its time: a paused address keeps its first pause.

Locks, in the lock order (``retention.lock_repositories``): the user rows in ID order, FOR NO KEY
UPDATE, then the address. The log names the outbox row and the event, never an address or the
body.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, literal, select, update
from sqlalchemy.orm import Session, sessionmaker

from burnbird.email.webhook_signature import is_valid
from burnbird.models import EmailOutbox, GithubUser, UserEmail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email")

HARD_BOUNCE: Final = "hardbounce"


@dataclass(frozen=True, slots=True)
class EmailWebhookState:
    """What the endpoint needs: the Authentication Key, the database sessions and a clock."""

    key: bytes
    sessions: sessionmaker[Session]
    now: Callable[[], datetime]


class _Model(BaseModel):
    # Only the fields read here; the provider's others are ignored.
    model_config = ConfigDict(extra="ignore", frozen=True)


class EventData(_Model):
    """What happened: ``object`` names the event.

    ``details`` is read only for an event acted on, and only for the address fields named in
    ``_addresses``, so an ignored event of another shape does not refuse the whole body.
    """

    object: str
    details: Any = ()


class EmailInfo(_Model):
    """The email the event is about; a reference that is not a UUID string is ignored."""

    client_reference: Any = None


class EventMessage(_Model):
    """One email and what happened to it."""

    email_info: EmailInfo
    event_data: tuple[EventData, ...] = ()


class Payload(_Model):
    """A webhook body: one or more emails."""

    event_message: tuple[EventMessage, ...]


@dataclass(frozen=True, slots=True)
class _Report:
    outbox_id: uuid.UUID
    user_id: int
    event: str
    addresses: frozenset[str]


def webhook_state(request: Request) -> EmailWebhookState:
    """Return the state ``burnbird.web.create_app`` put on the app."""
    state = request.app.state.email_webhook
    if not isinstance(state, EmailWebhookState):
        raise TypeError("the app has no email webhook state")
    return state


async def signed_body(request: Request) -> bytes:
    """Return the raw body once the provider's signature over it is valid; 403 otherwise.

    The one ``async def`` here: it awaits the body and makes no blocking call. The endpoint
    declares no body parameter, so nothing is parsed before this check. A refusal is logged with
    what came (its Content-Type and length), never what it held, so a scheme that differs from
    the documented one shows in the log.
    """
    body = await request.body()
    state = webhook_state(request)
    if not is_valid(state.key, body, request.headers.get("producer-signature"), state.now()):
        logger.warning(
            "email webhook: refused, the signature does not match (%s, %d bytes)",
            request.headers.get("content-type", "no content type"),
            len(body),
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the signature does not match")
    return body


@router.post("/webhook")
def receive_webhook(request: Request, body: Annotated[bytes, Depends(signed_body)]) -> Response:
    """Apply what the provider reports, in one transaction, and answer 200."""
    try:
        payload = Payload.model_validate_json(body)
    except ValidationError as err:
        # Field locations only: pydantic's messages would repeat values from the body.
        locations = [error["loc"] for error in err.errors(include_input=False)]
        logger.warning("email webhook: the body fails its schema at %s", locations)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the body fails its schema") from None
    state = webhook_state(request)
    with state.sessions.begin() as session:
        apply(session, payload, state.now())
    return Response(status_code=status.HTTP_200_OK)


def apply(session: Session, payload: Payload, now: datetime) -> None:
    """Apply each report about one of our emails in the caller's transaction."""
    reports = _reports(session, payload)
    if not reports:
        return
    # The user rows first, in ID order, as Delete my data, the purge and a sign-in take them.
    session.execute(
        select(GithubUser.id)
        .where(GithubUser.id.in_({report.user_id for report in reports}))
        .order_by(GithubUser.id)
        .with_for_update(key_share=True)
    ).all()
    # Read again under that lock: an email deleted meanwhile (Delete my data, a revoke, an
    # uninstall) is gone, and the user it named may be another account by now.
    present = set(
        session.scalars(
            select(EmailOutbox.id).where(EmailOutbox.id.in_({r.outbox_id for r in reports}))
        )
    )
    for report in sorted(reports, key=lambda report: report.user_id):
        if report.outbox_id not in present:
            logger.info("email webhook: outbox %s is deleted", report.outbox_id)
            continue
        paused = _pause(session, report, "hard_bounce", now)
        logger.warning("email webhook: %s for outbox %s", report.event, report.outbox_id)
        logger.info(
            "email webhook: outbox %s, user %d: %s",
            report.outbox_id,
            report.user_id,
            "paused" if paused else "no pause (already paused, or another address now)",
        )


def _reports(session: Session, payload: Payload) -> list[_Report]:
    """Return the reports this endpoint acts on; log and drop the rest."""
    reports = []
    for message in payload.event_message:
        reference = _uuid(message.email_info.client_reference)
        user_id = (
            None
            if reference is None
            else session.scalar(select(EmailOutbox.user_id).where(EmailOutbox.id == reference))
        )
        for data in message.event_data:
            if reference is None or user_id is None or data.object != HARD_BOUNCE:
                logger.info("email webhook: %s ignored (outbox %s)", data.object, reference)
                continue
            addresses = _addresses(data.details, "bounced_recipient")
            reports.append(_Report(reference, user_id, data.object, addresses))
    return reports


def _addresses(details: Any, *names: str) -> frozenset[str]:
    """Return the addresses the details name under ``names``; anything else is skipped."""
    items = details if isinstance(details, list | tuple) else ()
    return frozenset(
        value
        for item in items
        if isinstance(item, dict)
        for name in names
        if isinstance(value := item.get(name), str) and value
    )


def _pause(session: Session, report: _Report, reason: str, now: datetime) -> bool:
    """Pause the user's address if it is one the report names and is not paused yet."""
    paused = session.execute(
        update(UserEmail)
        .where(
            UserEmail.user_id == report.user_id,
            UserEmail.suppressed_at.is_(None),
            # Both sides lower-cased by the database, so they agree on every character.
            func.lower(UserEmail.address).in_(
                [func.lower(literal(address)) for address in report.addresses]
            ),
        )
        .values(suppressed_at=now, suppressed_reason=reason)
        .returning(UserEmail.user_id)
    ).first()
    return paused is not None


def _uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
