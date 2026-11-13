"""``POST /github/webhook``: check the signature, keep a handled event's fields, answer at once.

The order is fixed. The app's body limit refuses an oversized body while it is read (413), the
signature over the raw body is checked before anything in it is read (403), and only then is the
body parsed. A handled event's fields are stored for the worker (``burnbird.github.events``) and
answered with 202, a repeated delivery too; any other event is answered with 204 and dropped.
GitHub expects a 2XX answer within 10 seconds:
https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird.github import schemas
from burnbird.github.signature import is_valid
from burnbird.models import WebhookDelivery

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass(frozen=True, slots=True)
class WebhookState:
    """What the endpoint needs from the app: the webhook secret and the database sessions."""

    secret: bytes
    sessions: sessionmaker[Session]


def webhook_state(request: Request) -> WebhookState:
    """Return the state ``burnbird.web.create_app`` put on the app."""
    state = request.app.state.webhook
    if not isinstance(state, WebhookState):
        raise TypeError("the app has no webhook state")
    return state


async def signed_body(request: Request) -> bytes:
    """Return the raw body once GitHub's signature over it is valid; 403 otherwise.

    The one ``async def`` here: it awaits the body and makes no blocking call. The endpoint
    declares no body parameter, so FastAPI parses nothing before this check.
    """
    body = await request.body()
    if not is_valid(
        webhook_state(request).secret, body, request.headers.get("X-Hub-Signature-256")
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the signature does not match")
    return body


@router.post("/github/webhook")
def receive_webhook(request: Request, body: Annotated[bytes, Depends(signed_body)]) -> Response:
    """Store a handled delivery's fields once and answer 202; answer any other event 204."""
    delivery = _delivery_id(request.headers.get("X-GitHub-Delivery"))
    event = request.headers.get("X-GitHub-Event")
    if not event:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-GitHub-Event is missing")
    data = _json_object(body)
    action = data.get("action")
    if not isinstance(action, str) or (model := schemas.HANDLED.get((event, action))) is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        payload = model.model_validate(data)
    except ValidationError as err:
        # Field locations only: pydantic's messages would repeat values from the body.
        locations = [error["loc"] for error in err.errors(include_input=False)]
        logger.warning(
            "delivery %s: %s %s fails its schema at %s", delivery, event, action, locations
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the payload fails its schema") from None
    with webhook_state(request).sessions.begin() as session:
        session.execute(
            insert(WebhookDelivery)
            .values(
                id=delivery,
                received_at=datetime.now(UTC),
                event=event,
                action=action,
                payload=payload.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=[WebhookDelivery.id])
        )
    # A redelivery keeps its X-GitHub-Delivery ID; it is stored once and answered like the first.
    return Response(status_code=status.HTTP_202_ACCEPTED)


def _delivery_id(header: str | None) -> uuid.UUID:
    if header is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-GitHub-Delivery is missing")
    try:
        return uuid.UUID(header)
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "X-GitHub-Delivery is not a GUID"
        ) from None


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the body is not JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the body is not a JSON object")
    return data
