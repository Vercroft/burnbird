"""``/you``: what it shows, the run-out warnings switch, and Delete my data.

Every address is invented on example.invalid at run time; logins and IDs are invented.
"""

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from burnbird.auth.config import AuthSettings
from burnbird.auth.service import SESSION_COOKIE
from burnbird.email import consent
from burnbird.email.preferences import set_choice
from burnbird.models import (
    Account,
    EmailConsent,
    EmailPreference,
    GithubUser,
    UserEmail,
    UserSession,
)
from tests.auth.conftest import (
    INSTALLATION,
    ORG,
    OTHER_ORG,
    USER,
    Clock,
    FakeGithub,
    email_item,
    installation_item,
    invented_address,
    repository_item,
)

type SignIn = Callable[[], httpx2.Response]

ORIGIN: Final = "https://burnbird.test"
NOW: Final = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
FIREFOX: Final = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
# A second, invented user in another browser, whose rows must stay untouched and unseen.
OTHER_USER: Final = 7002
OTHER_LOGIN: Final = "example-other-person"
EDGE: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141"
    + ".0" * 3
    + " Safari/537.36 Edg/141"
    + ".0" * 3
)
NO_ADDRESS: Final = (
    "Burnbird has no email address for you. It stores your GitHub primary address when GitHub "
    "marks it verified and lets Burnbird read it. To get run-out warnings by email, approve "
    "Burnbird's 'Email addresses' permission on GitHub if asked, then sign in again."
)
DELETE_WORDING: Final = (
    "While the App stays installed, new jobs are collected again. Uninstall the App to stop "
    "collection."
)


@pytest.fixture(autouse=True)
def fixed_clock(clock: Clock) -> None:
    clock.fixed = NOW


@pytest.fixture
def address(github: FakeGithub) -> str:
    chosen = invented_address()
    github.emails = [email_item(chosen)]
    github.installations = [installation_item(INSTALLATION[ORG], ORG)]
    github.repositories = {INSTALLATION[ORG]: [repository_item(5001)]}
    return chosen


@pytest.fixture
def signed_in(client: TestClient, sign_in: SignIn, address: str) -> str:
    """Sign in from Firefox on Linux and return the page's CSRF token."""
    client.headers["user-agent"] = FIREFOX
    assert sign_in().status_code == 303
    return _csrf(client)


@pytest.fixture
def other_user(app: FastAPI, github: FakeGithub, auth_settings: AuthSettings) -> Iterator[str]:
    """Sign a second user in from their own browser and account; yield their address."""
    theirs = invented_address()
    kept = (github.user, github.emails, github.installations, github.repositories)
    github.user = {"id": OTHER_USER, "login": OTHER_LOGIN, "type": "User"}
    github.emails = [email_item(theirs)]
    github.installations = [installation_item(INSTALLATION[OTHER_ORG], OTHER_ORG)]
    github.repositories = {INSTALLATION[OTHER_ORG]: [repository_item(5002)]}
    with TestClient(app, base_url=auth_settings.origin, follow_redirects=False) as browser:
        browser.headers["user-agent"] = EDGE
        github_sign_in(browser, github)
        github.user, github.emails, github.installations, github.repositories = kept
        yield theirs


# Each table that holds a user's rows, and the column naming the user.
_OWNED: Final = {
    "github_user": GithubUser.id,
    "user_email": UserEmail.user_id,
    "session": UserSession.user_id,
    "email_preference": EmailPreference.user_id,
    "email_consent": EmailConsent.user_id,
}


def _rows_of(session: Session, user: int) -> dict[str, int]:
    session.expire_all()
    return {
        name: len(session.execute(select(column).where(column == user)).all())
        for name, column in _OWNED.items()
    }


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/you").text)
    assert match is not None
    return match[1]


def _text(html: str) -> str:
    """The page's words, tags removed and spaces folded, as a reader sees them."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _choices(session: Session) -> dict[str, bool]:
    session.expire_all()
    return {
        row.kind: row.enabled
        for row in session.scalars(select(EmailPreference).where(EmailPreference.user_id == USER))
    }


def _consents(session: Session) -> list[tuple[str, bool, str, str]]:
    session.expire_all()
    return [
        (row.kind, row.enabled, row.source, row.text_version)
        for row in session.scalars(
            select(EmailConsent).where(EmailConsent.user_id == USER).order_by(EmailConsent.id)
        )
    ]


def _save(client: TestClient, token: str, *, on: bool) -> httpx2.Response:
    form = {"csrf_token": token} | ({"usage_warnings": "on"} if on else {})
    return client.post("/you/email", data=form, headers={"Origin": ORIGIN})


# ---------- the page ----------


def test_without_a_session_the_page_sends_the_visitor_to_sign_in(client: TestClient) -> None:
    response = client.get("/you")
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_the_page_lists_what_is_stored(
    client: TestClient, signed_in: str, address: str, session: Session
) -> None:
    response = client.get("/you")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    text = _text(response.text)
    assert f"Your address: {address}, read from GitHub on 15 Nov 2026, 09:00 UTC." in text
    assert "Warnings continue until 15 Dec 2026. Sign in to keep them." in text
    assert consent.TEXT in text
    assert "Firefox 140 on Linux, computer, signed in on 15 Nov 2026, 09:00 UTC" in text
    assert f"example-{ORG}" in text
    assert DELETE_WORDING in text
    assert 'href="https://github.com/Vercroft/burnbird/blob/main/PRIVACY.md"' in response.text
    assert 'name="usage_warnings" value="on" checked' in response.text
    assert NO_ADDRESS not in text


def test_the_page_shows_nothing_of_another_user(
    client: TestClient, other_user: str, signed_in: str
) -> None:
    text = _text(client.get("/you").text)
    assert other_user not in text
    assert "Edge 141" not in text
    assert f"example-{OTHER_ORG}" not in text
    assert text.count(", signed in on ") == 1


def test_only_the_run_out_warnings_switch_is_shown(client: TestClient, signed_in: str) -> None:
    html = client.get("/you").text
    # The one email switch, and the Delete my data form's confirm box.
    assert re.findall(r'type="checkbox" name="([^"]+)"', html) == [
        "usage_warnings",
        "confirm_delete",
    ]
    assert "weekly" not in html.lower()
    assert "news" not in html.lower()


def test_a_user_without_an_address_gets_the_one_wording(
    client: TestClient, github: FakeGithub, sign_in: SignIn
) -> None:
    github.emails = []
    assert sign_in().status_code == 303
    text = _text(client.get("/you").text)
    assert NO_ADDRESS in text
    assert "Warnings continue until" not in text
    # No address was ever stored, so no choice exists and no switch is shown.
    assert "Run-out warnings" not in text


def test_an_address_deleted_later_keeps_the_switch_and_shows_the_wording(
    client: TestClient, github: FakeGithub, signed_in: str
) -> None:
    github.emails = []
    client.post("/auth/logout", data={"csrf_token": signed_in}, headers={"Origin": ORIGIN})
    github_sign_in(client, github)
    html = client.get("/you").text
    assert NO_ADDRESS in _text(html)
    assert 'name="usage_warnings"' in html


def github_sign_in(client: TestClient, github: FakeGithub) -> None:
    login = client.get("/auth/login")
    code, state = github.authorize(login.headers["location"])
    assert client.get("/auth/callback", params={"code": code, "state": state}).status_code == 303


def test_a_value_with_markup_is_shown_as_text(
    client: TestClient, signed_in: str, session: Session
) -> None:
    account = session.get_one(Account, ORG)
    account.login = "<b>example</b>"
    session.flush()
    html = client.get("/you").text
    assert "&lt;b&gt;example&lt;/b&gt;" in html
    assert "<b>example</b>" not in html


# ---------- the switch ----------


def test_the_first_address_writes_the_defaults_and_their_consent_rows(
    signed_in: str, session: Session
) -> None:
    assert _choices(session) == {"usage_warnings": True, "weekly_summary": False, "news": False}
    assert _consents(session) == [
        ("usage_warnings", True, "default", consent.TEXT_VERSION),
        ("weekly_summary", False, "default", consent.TEXT_VERSION),
        ("news", False, "default", consent.TEXT_VERSION),
    ]


def test_each_change_writes_one_consent_row_and_a_repeat_none(
    client: TestClient, signed_in: str, session: Session, clock: Clock
) -> None:
    defaults = _consents(session)
    clock.fixed = NOW + timedelta(minutes=1)
    assert _save(client, signed_in, on=False).headers["location"] == "/you"
    assert _save(client, signed_in, on=False).status_code == 303
    assert _choices(session)["usage_warnings"] is False
    assert _consents(session) == [
        *defaults,
        ("usage_warnings", False, "settings", consent.TEXT_VERSION),
    ]
    assert _save(client, signed_in, on=True).status_code == 303
    assert _save(client, signed_in, on=True).status_code == 303
    assert _choices(session)["usage_warnings"] is True
    assert len(_consents(session)) == len(defaults) + 2
    assert 'name="usage_warnings" value="on" checked' in client.get("/you").text


def test_with_warnings_off_the_page_promises_no_warnings(
    client: TestClient, signed_in: str
) -> None:
    assert "Warnings continue until" in client.get("/you").text
    _save(client, signed_in, on=False)
    html = client.get("/you").text
    assert "Warnings continue until" not in html
    assert 'name="usage_warnings" value="on">' in html


def test_warnings_turned_off_by_the_email_link_say_so_until_the_switch_turns_them_on(
    client: TestClient, signed_in: str, session: Session
) -> None:
    set_choice(session, USER, "usage_warnings", False, NOW + timedelta(days=1), source="one_click")
    session.flush()
    turned_off = "Run-out warnings: off, turned off by the email link on "
    assert turned_off + _day(NOW + timedelta(days=1)) in _text(client.get("/you").text)
    _save(client, signed_in, on=True)
    assert turned_off not in _text(client.get("/you").text)
    assert _choices(session)["usage_warnings"] is True


def test_warnings_turned_off_on_the_page_do_not_name_the_email_link(
    client: TestClient, signed_in: str
) -> None:
    _save(client, signed_in, on=False)
    assert "turned off by the email link" not in client.get("/you").text


def _day(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%d %b %Y")


def test_a_later_sign_in_never_resets_a_choice(
    client: TestClient, github: FakeGithub, signed_in: str, session: Session
) -> None:
    _save(client, signed_in, on=False)
    before = _consents(session)
    # The address goes (GitHub shares none), then comes back: the choice stays.
    github.emails = []
    github_sign_in(client, github)
    github.emails = [email_item(invented_address())]
    github_sign_in(client, github)
    assert _choices(session)["usage_warnings"] is False
    assert _consents(session) == before


@pytest.mark.parametrize(
    ("form", "origin"),
    [({}, ORIGIN), (None, "https://example.com"), (None, None)],
    ids=["no token", "another site", "no origin"],
)
def test_the_switch_needs_the_session_s_token_and_this_site(
    form: dict[str, str] | None,
    origin: str | None,
    client: TestClient,
    signed_in: str,
    session: Session,
) -> None:
    data = {"csrf_token": signed_in} if form is None else form
    headers = {} if origin is None else {"Origin": origin}
    assert client.post("/you/email", data=data, headers=headers).status_code == 403
    assert _choices(session)["usage_warnings"] is True
    assert len(_consents(session)) == 3


# ---------- delete my data ----------


def test_delete_my_data_removes_everything_and_ends_the_session(
    client: TestClient,
    app: FastAPI,
    github: FakeGithub,
    other_user: str,
    signed_in: str,
    session: Session,
) -> None:
    # The same user in a second browser: that session goes too.
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as second:
        github_sign_in(second, github)
    assert _rows_of(session, USER)["session"] == 2
    others_before = _rows_of(session, OTHER_USER)
    response = client.post(
        "/you/delete",
        data={"csrf_token": signed_in, "confirm_delete": "yes"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/you/deleted"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "Max-Age=0" in cookie or "1970" in cookie
    attributes = {part.strip().lower() for part in cookie.split(";")}
    assert {"secure", "path=/", "httponly"} <= attributes
    assert _rows_of(session, USER) == dict.fromkeys(others_before, 0)
    assert _rows_of(session, OTHER_USER) == others_before
    assert others_before == {
        "github_user": 1,
        "user_email": 1,
        "session": 1,
        "email_preference": 3,
        "email_consent": 3,
    }
    page = client.get("/you/deleted")
    assert page.status_code == 200
    assert "Your data is deleted" in page.text
    assert client.get("/you").headers["location"] == "/auth/login"


@pytest.mark.parametrize("origin", [ORIGIN, "https://example.com"], ids=["no token", "other site"])
def test_delete_my_data_needs_the_session_s_token_and_this_site(
    origin: str, client: TestClient, other_user: str, signed_in: str, session: Session
) -> None:
    before = (_rows_of(session, USER), _rows_of(session, OTHER_USER))
    data = {} if origin == ORIGIN else {"csrf_token": signed_in}
    assert client.post("/you/delete", data=data, headers={"Origin": origin}).status_code == 403
    assert (_rows_of(session, USER), _rows_of(session, OTHER_USER)) == before


def test_the_delete_form_asks_to_confirm(client: TestClient, signed_in: str) -> None:
    html = client.get("/you").text
    form = html[html.index('action="/you/delete"') :]
    form = form[: form.index("</form>")]
    assert re.search(r'<input type="checkbox" name="confirm_delete" value="yes" required>', form)
    assert "I understand this cannot be undone." in _text(form)


@pytest.mark.parametrize(
    "confirm",
    ["", "&confirm_delete=no", "&confirm_delete=yes&confirm_delete=yes"],
    ids=["missing", "no", "sent twice"],
)
def test_delete_my_data_without_the_confirm_box_deletes_nothing(
    confirm: str, client: TestClient, other_user: str, signed_in: str, session: Session
) -> None:
    before = (_rows_of(session, USER), _rows_of(session, OTHER_USER))
    response = client.post(
        "/you/delete",
        content=f"csrf_token={signed_in}{confirm}",
        headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    assert "set-cookie" not in response.headers
    assert (_rows_of(session, USER), _rows_of(session, OTHER_USER)) == before


def test_a_repeated_warnings_field_is_refused(
    client: TestClient, signed_in: str, session: Session
) -> None:
    before = (_choices(session), _consents(session))
    response = client.post(
        "/you/email",
        content=f"csrf_token={signed_in}&usage_warnings=on&usage_warnings=on",
        headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    assert (_choices(session), _consents(session)) == before
    # The switch was on; turning it off with the same request shape still works once.
    assert _save(client, signed_in, on=False).status_code == 303
    assert _choices(session)["usage_warnings"] is False


@pytest.mark.parametrize("value", ["yes", "off"])
def test_a_warnings_field_that_is_not_on_is_refused(
    value: str, client: TestClient, signed_in: str, session: Session
) -> None:
    before = (_choices(session), _consents(session))
    response = client.post(
        "/you/email",
        data={"csrf_token": signed_in, "usage_warnings": value},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 400
    assert (_choices(session), _consents(session)) == before
