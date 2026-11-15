"""Each user's email choices: written once with defaults, then changed only by the user.

``email_preference`` holds the current choice per kind; ``email_consent`` is the append-only log of
every choice, with the wording's version, so a choice can be shown later. Functions never commit:
the caller runs each in its transaction.
"""

from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from burnbird.email.consent import TEXT_VERSION
from burnbird.models import EmailConsent, EmailPreference

# Run-out warnings are on unless turned off; the weekly summary and news only if asked for.
# One entry per kind in ``models.EMAIL_KINDS`` (a test checks they agree).
DEFAULTS: Final = {"usage_warnings": True, "weekly_summary": False, "news": False}


def write_defaults(session: Session, user_id: int, now: datetime) -> int:
    """Write the default choice of every kind the user has none for; return how many.

    An existing choice is never reset, so an address that is deleted and read again keeps the
    user's choices. Each default written gets its consent row (source ``default``).
    """
    written = list(
        session.scalars(
            insert(EmailPreference)
            .values(
                [
                    {"user_id": user_id, "kind": kind, "enabled": enabled, "updated_at": now}
                    for kind, enabled in DEFAULTS.items()
                ]
            )
            .on_conflict_do_nothing(index_elements=[EmailPreference.user_id, EmailPreference.kind])
            .returning(EmailPreference.kind)
        )
    )
    for kind in written:
        _record(session, user_id, kind, DEFAULTS[kind], "default", now)
    return len(written)


def set_choice(session: Session, user_id: int, kind: str, enabled: bool, now: datetime) -> bool:
    """Set one choice the user made; return whether it changed.

    The row is locked first, so two saves at once record one change. A repeat changes nothing and
    writes no consent row. A user with no row (no address was ever stored) changes nothing.
    """
    current = session.scalar(
        select(EmailPreference)
        .where(EmailPreference.user_id == user_id, EmailPreference.kind == kind)
        .with_for_update()
    )
    if current is None or current.enabled == enabled:
        return False
    current.enabled = enabled
    current.updated_at = now
    _record(session, user_id, kind, enabled, "settings", now)
    return True


def _record(
    session: Session, user_id: int, kind: str, enabled: bool, source: str, now: datetime
) -> None:
    session.add(
        EmailConsent(
            user_id=user_id,
            kind=kind,
            enabled=enabled,
            source=source,
            text_version=TEXT_VERSION,
            at=now,
        )
    )
