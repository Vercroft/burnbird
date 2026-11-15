"""What the "Your data" page shows, read in one transaction; no function here commits."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from burnbird.models import (
    Account,
    EmailPreference,
    Installation,
    UserEmail,
    UserInstallation,
    UserSession,
)


@dataclass(frozen=True, slots=True)
class SignedInSession:
    """One live session: its device label (None for one started before labels) and start."""

    device: str | None
    started_at: datetime


@dataclass(frozen=True, slots=True)
class YourData:
    """Everything stored about the user that the page lists."""

    address: str | None
    read_at: datetime | None
    # The run-out warnings choice; None when no address was ever stored, so no choice exists.
    warnings: bool | None
    sessions: tuple[SignedInSession, ...]
    # The logins of the accounts the user could see at their last sign-in.
    accounts: tuple[str, ...]


def your_data(session: Session, user_id: int, now: datetime) -> YourData:
    """Read what the page shows about the user."""
    email = session.get(UserEmail, user_id)
    warnings = session.scalar(
        select(EmailPreference.enabled).where(
            EmailPreference.user_id == user_id, EmailPreference.kind == "usage_warnings"
        )
    )
    sessions = session.execute(
        select(UserSession.device, UserSession.created_at)
        .where(UserSession.user_id == user_id, UserSession.expires_at > now)
        .order_by(UserSession.created_at)
    ).all()
    accounts = session.scalars(
        select(Account.login)
        .join(Installation, Installation.account_id == Account.id)
        .join(UserInstallation, UserInstallation.installation_id == Installation.id)
        .where(UserInstallation.user_id == user_id)
        .order_by(Account.login)
    )
    return YourData(
        address=None if email is None else email.address,
        read_at=None if email is None else email.read_at,
        warnings=warnings,
        sessions=tuple(SignedInSession(device, started) for device, started in sessions),
        accounts=tuple(accounts),
    )
