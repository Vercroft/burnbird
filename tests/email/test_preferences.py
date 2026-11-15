"""The email choice rules: defaults written once, a change recorded once, a repeat never."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from burnbird.email.consent import TEXT_VERSION
from burnbird.email.preferences import set_choice, write_defaults
from burnbird.models import EmailConsent, EmailPreference, GithubUser

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
USER = 7401


@pytest.fixture
def user(session: Session) -> int:
    session.add(GithubUser(id=USER, login="example-chooser", last_sign_in_at=NOW))
    session.flush()
    return USER


def _choices(session: Session) -> dict[str, bool]:
    session.flush()
    session.expire_all()
    rows = session.scalars(select(EmailPreference).where(EmailPreference.user_id == USER))
    return {row.kind: row.enabled for row in rows}


def _consents(session: Session) -> list[tuple[str, bool, str, str, datetime]]:
    session.flush()
    session.expire_all()
    rows = session.scalars(
        select(EmailConsent).where(EmailConsent.user_id == USER).order_by(EmailConsent.id)
    )
    return [(r.kind, r.enabled, r.source, r.text_version, r.at) for r in rows]


def test_the_defaults_and_their_consent_rows_are_written_once(session: Session, user: int) -> None:
    assert write_defaults(session, user, NOW) == 3
    assert _choices(session) == {"usage_warnings": True, "weekly_summary": False, "news": False}
    assert _consents(session) == [
        ("usage_warnings", True, "default", TEXT_VERSION, NOW),
        ("weekly_summary", False, "default", TEXT_VERSION, NOW),
        ("news", False, "default", TEXT_VERSION, NOW),
    ]
    assert write_defaults(session, user, NOW + timedelta(days=1)) == 0
    assert len(_consents(session)) == 3


def test_defaults_never_reset_a_choice_the_user_made(session: Session, user: int) -> None:
    write_defaults(session, user, NOW)
    assert set_choice(session, user, "usage_warnings", False, NOW + timedelta(minutes=1))
    before = _consents(session)
    assert write_defaults(session, user, NOW + timedelta(days=1)) == 0
    assert _choices(session)["usage_warnings"] is False
    assert _consents(session) == before


def test_a_change_writes_one_consent_row_and_a_repeat_none(session: Session, user: int) -> None:
    write_defaults(session, user, NOW)
    later = NOW + timedelta(minutes=1)
    assert set_choice(session, user, "usage_warnings", False, later)
    assert not set_choice(session, user, "usage_warnings", False, later)
    assert _choices(session)["usage_warnings"] is False
    assert _consents(session)[3:] == [("usage_warnings", False, "settings", TEXT_VERSION, later)]
    assert set_choice(session, user, "usage_warnings", True, later)
    assert not set_choice(session, user, "usage_warnings", True, later)
    assert len(_consents(session)) == 5


def test_a_user_with_no_choices_changes_nothing(session: Session, user: int) -> None:
    assert not set_choice(session, user, "usage_warnings", False, NOW)
    assert _choices(session) == {}
    assert _consents(session) == []
