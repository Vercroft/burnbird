"""One-click unsubscribe: an email's link turns its kind of email off.

A usage email's link holds a random token; only the SHA-256 of it is stored, on its outbox row,
as a session's ID is. The token decides everything: the request's body, cookies and headers are
never read, as RFC 8058 says a mail client's POST carries no other context.
"""

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from burnbird.email.preferences import set_choice
from burnbird.models import EmailOutbox


def turn_off(session: Session, token: str, now: datetime) -> bool:
    """Turn off the kind of email the token's message was; return whether a choice changed.

    An unknown token, or one already used, changes nothing. The choice is recorded with the
    source ``one_click``, so the "Your data" page can say the link turned it off.
    """
    found = session.execute(
        select(EmailOutbox.user_id, EmailOutbox.kind).where(
            EmailOutbox.unsubscribe_sha256 == hashlib.sha256(token.encode()).digest()
        )
    ).first()
    if found is None or found.kind != "usage_warnings":
        return False
    return set_choice(session, found.user_id, found.kind, False, now, source="one_click")
