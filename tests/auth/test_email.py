"""Sign-in reads the user's primary verified email address, and labels the session's device.

Every address is invented on example.invalid at run time; no account is real.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from burnbird.email.preferences import set_choice
from burnbird.models import EmailConsent, EmailPreference, UserEmail, UserSession
from tests.auth.conftest import USER, Clock, FakeGithub, email_item, invented_address

type SignIn = Callable[[], httpx2.Response]

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
FIREFOX_ON_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


@pytest.fixture(autouse=True)
def fixed_clock(clock: Clock) -> None:
    clock.fixed = NOW


def _stored(session: Session) -> UserEmail | None:
    session.expire_all()
    return session.get(UserEmail, USER)


def _signed_in(sign_in: SignIn) -> None:
    assert sign_in().status_code == 303


def test_the_primary_verified_address_is_stored_with_its_read_time(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    address = invented_address()
    github.emails = [email_item(invented_address(), primary=False), email_item(address)]
    _signed_in(sign_in)
    stored = _stored(session)
    assert stored is not None
    assert (stored.address, stored.read_at) == (address, NOW)


@pytest.mark.parametrize(
    "emails",
    [
        [],
        [email_item("unverified-" + "@".join(("x", "example.invalid")), verified=False)],
        [email_item("secondary-" + "@".join(("x", "example.invalid")), primary=False)],
    ],
    ids=["none", "primary not verified", "verified but not primary"],
)
def test_no_primary_verified_address_stores_none_and_deletes_the_old_one(
    emails: list[dict[str, object]], github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    _signed_in(sign_in)
    assert _stored(session) is not None
    github.emails = emails
    _signed_in(sign_in)
    assert _stored(session) is None


@pytest.mark.parametrize("status", [403, 404])
def test_no_permission_to_read_addresses_deletes_the_stored_one(
    status: int, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    _signed_in(sign_in)
    assert _stored(session) is not None
    github.answers["/user/emails"] = httpx2.Response(
        status, json={"message": "Resource not accessible by integration"}
    )
    _signed_in(sign_in)
    assert _stored(session) is None


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(500, json={"message": "Server Error"}),
        httpx2.Response(403, headers={"retry-after": "60"}, json={"message": "slow down"}),
        httpx2.Response(
            403, json={"message": "You have exceeded a secondary rate limit. Please wait."}
        ),
        httpx2.Response(200, json={"not": "a list"}),
        httpx2.Response(200, json=[{"email": 1}]),
    ],
    ids=["server error", "rate limit with retry-after", "secondary limit", "object", "bad item"],
)
def test_an_unreadable_answer_keeps_the_stored_address_and_the_sign_in_succeeds(
    answer: httpx2.Response,
    github: FakeGithub,
    sign_in: SignIn,
    session: Session,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    address = github.emails[0]["email"]
    _signed_in(sign_in)
    github.answers["/user/emails"] = answer
    clock.fixed = NOW + timedelta(hours=9)
    with caplog.at_level(logging.WARNING):
        _signed_in(sign_in)
    stored = _stored(session)
    assert stored is not None
    # Kept as it was: it was not read again, so its read time does not move.
    assert (stored.address, stored.read_at) == (address, NOW)
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "the stored one is kept" in warning.getMessage()


def test_a_timed_out_read_keeps_the_stored_address(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    _signed_in(sign_in)
    github.timeouts.add("/user/emails")
    _signed_in(sign_in)
    assert _stored(session) is not None


def test_the_address_is_found_on_a_later_page(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    address = invented_address()
    github.page_size = 1
    github.emails = [email_item(invented_address(), primary=False), email_item(address)]
    _signed_in(sign_in)
    stored = _stored(session)
    assert stored is not None
    assert stored.address == address


def test_a_changed_address_replaces_the_stored_one(
    github: FakeGithub, sign_in: SignIn, session: Session, clock: Clock
) -> None:
    _signed_in(sign_in)
    changed = invented_address()
    github.emails = [email_item(changed)]
    clock.fixed = NOW + timedelta(days=2)
    _signed_in(sign_in)
    stored = _stored(session)
    assert stored is not None
    assert (stored.address, stored.read_at) == (changed, NOW + timedelta(days=2))
    assert len(session.scalars(select(UserEmail)).all()) == 1


@pytest.mark.parametrize(
    "answer",
    [None, httpx2.Response(403, json={}), httpx2.Response(500, json={})],
    ids=["read", "no permission", "failed"],
)
def test_the_address_never_reaches_a_log(
    answer: httpx2.Response | None,
    github: FakeGithub,
    sign_in: SignIn,
    caplog: pytest.LogCaptureFixture,
) -> None:
    address = github.emails[0]["email"]
    caplog.set_level(logging.DEBUG)
    _signed_in(sign_in)
    if answer is not None:
        github.answers["/user/emails"] = answer
        _signed_in(sign_in)
    assert isinstance(address, str)
    assert address not in caplog.text


def test_the_session_is_labelled_with_its_device_and_never_the_raw_header(
    client: TestClient, sign_in: SignIn, session: Session
) -> None:
    client.headers["user-agent"] = FIREFOX_ON_LINUX
    _signed_in(sign_in)
    session.expire_all()
    [stored] = session.scalars(select(UserSession)).all()
    assert stored.device == "Firefox 140 on Linux, computer"


def _address_of_length(length: int) -> str:
    domain = "@" + "example.invalid"
    return "x" * (length - len(domain)) + domain


@pytest.mark.parametrize(
    "address",
    [_address_of_length(255), "@" + "unusable." + "example.invalid"],
    ids=["255 characters", "starts with @"],
)
def test_an_address_the_table_would_refuse_is_not_stored_and_the_sign_in_succeeds(
    address: str,
    github: FakeGithub,
    sign_in: SignIn,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _signed_in(sign_in)
    assert _stored(session) is not None
    github.emails = [email_item(address)]
    caplog.set_level(logging.DEBUG)
    _signed_in(sign_in)
    assert _stored(session) is None
    assert address not in caplog.text
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert (
        warning.getMessage() == "sign-in: GitHub's primary address is not usable; none is stored"
    )


def test_an_address_of_254_characters_is_stored(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    address = _address_of_length(254)
    github.emails = [email_item(address)]
    _signed_in(sign_in)
    stored = _stored(session)
    assert stored is not None
    assert stored.address == address


def _choice_rows(session: Session) -> tuple[dict[str, bool], int]:
    session.expire_all()
    choices = {
        row.kind: row.enabled
        for row in session.scalars(select(EmailPreference).where(EmailPreference.user_id == USER))
    }
    consents = session.scalars(select(EmailConsent).where(EmailConsent.user_id == USER)).all()
    return choices, len(consents)


def test_the_first_stored_address_writes_the_default_choices(
    sign_in: SignIn, session: Session
) -> None:
    _signed_in(sign_in)
    assert _choice_rows(session) == (
        {"usage_warnings": True, "weekly_summary": False, "news": False},
        3,
    )


def test_no_address_writes_no_choices(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    github.emails = []
    _signed_in(sign_in)
    assert _choice_rows(session) == ({}, 0)


def test_an_address_deleted_and_read_again_keeps_the_user_s_choices(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    _signed_in(sign_in)
    assert set_choice(session, USER, "usage_warnings", False, NOW)
    session.flush()
    github.emails = []
    _signed_in(sign_in)
    github.emails = [email_item(invented_address())]
    _signed_in(sign_in)
    assert _choice_rows(session) == (
        {"usage_warnings": False, "weekly_summary": False, "news": False},
        4,
    )
