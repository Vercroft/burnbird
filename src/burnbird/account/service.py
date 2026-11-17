"""What the "Your data" page shows and saves, in the caller's transaction; nothing here commits."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from burnbird.email.preferences import set_choice
from burnbird.models import (
    Account,
    EmailConsent,
    EmailPreference,
    GithubUser,
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
    # When an email's one-click link turned the warnings off, if that is why they are off now: a
    # link checker can follow it unseen, so the page says so.
    warnings_off_by_link: datetime | None
    # Why and since when email to the address is paused (a hard bounce, a spam report), if it is.
    paused_reason: str | None
    paused_at: datetime | None
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
    last_choice = session.execute(
        select(EmailConsent.source, EmailConsent.at)
        .where(EmailConsent.user_id == user_id, EmailConsent.kind == "usage_warnings")
        .order_by(EmailConsent.id.desc())
        .limit(1)
    ).first()
    off_by_link = (
        last_choice.at
        if warnings is False and last_choice is not None and last_choice.source == "one_click"
        else None
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
        warnings_off_by_link=off_by_link,
        paused_reason=None if email is None else email.suppressed_reason,
        paused_at=None if email is None else email.suppressed_at,
        sessions=tuple(SignedInSession(device, started) for device, started in sessions),
        accounts=tuple(accounts),
    )


def save_warnings(session: Session, user_id: int, enabled: bool, now: datetime) -> bool:
    """Save the run-out warnings switch; return whether the choice changed.

    Turning warnings on is the user's new consent after a spam complaint, so it lifts a pause the
    complaint set; a bounce's pause stays until a sign-in brings a new address. The user row is
    taken first (FOR NO KEY UPDATE), then the address, then the choice: the provider's webhook
    takes them in that order too (``retention.lock_repositories``).
    """
    session.execute(
        select(GithubUser.id).where(GithubUser.id == user_id).with_for_update(key_share=True)
    ).all()
    if enabled:
        session.execute(
            update(UserEmail)
            .where(UserEmail.user_id == user_id, UserEmail.suppressed_reason == "complaint")
            .values(suppressed_at=None, suppressed_reason=None)
        )
    return set_choice(session, user_id, "usage_warnings", enabled, now)
