import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.cookies import Morsel, SimpleCookie

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from burnbird.auth.service import (
    OAUTH_COOKIE,
    OAUTH_COOKIE_SECONDS,
    SESSION_COOKIE,
    SESSION_LIFETIME,
)
from burnbird.models import (
    Account,
    GithubUser,
    Installation,
    Repository,
    UserInstallation,
    UserRepository,
    UserSession,
)
from burnbird.retention import revoke_authorization
from tests.auth.conftest import (
    INSTALLATION,
    ORG,
    OTHER_ORG,
    USER,
    Clock,
    FakeGithub,
    installation_item,
    repository_item,
)

type SignIn = Callable[[], httpx2.Response]

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _cookie(response: httpx2.Response, name: str) -> Morsel[str]:
    for header in response.headers.get_list("set-cookie"):
        parsed: SimpleCookie = SimpleCookie()
        parsed.load(header)
        if name in parsed:
            return parsed[name]
    raise AssertionError(f"no Set-Cookie for {name}")


def _host_only_and_locked_down(morsel: Morsel[str]) -> None:
    # MDN: a __Host- cookie must be Secure, have Path=/ and no Domain.
    assert morsel.key.startswith("__Host-")
    assert morsel["secure"] is True
    assert morsel["httponly"] is True
    assert morsel["samesite"].lower() == "lax"
    assert morsel["path"] == "/"
    assert morsel["domain"] == ""


def _org_with(github: FakeGithub, *repositories: int) -> None:
    github.installations = [installation_item(INSTALLATION[ORG], ORG)]
    github.repositories = {INSTALLATION[ORG]: [repository_item(r) for r in repositories]}


# ---------- cookie attributes ----------


def test_the_oauth_cookie_is_host_only_secure_and_short(
    client: TestClient, github: FakeGithub
) -> None:
    morsel = _cookie(client.get("/auth/login"), OAUTH_COOKIE)
    _host_only_and_locked_down(morsel)
    assert morsel["max-age"] == str(OAUTH_COOKIE_SECONDS) == "600"


def test_the_session_cookie_is_host_only_secure_and_lasts_eight_hours(
    github: FakeGithub, sign_in: SignIn
) -> None:
    _org_with(github, 5001)
    response = sign_in()
    morsel = _cookie(response, SESSION_COOKIE)
    _host_only_and_locked_down(morsel)
    assert morsel["max-age"] == str(int(SESSION_LIFETIME.total_seconds())) == "28800"
    # The callback's answer also clears the OAuth cookie, with the flags a __Host- cookie needs.
    cleared = _cookie(response, OAUTH_COOKIE)
    _host_only_and_locked_down(cleared)
    assert "1970" in cleared["expires"]


def test_sign_out_clears_the_session_cookie_with_the_same_flags(
    client: TestClient, github: FakeGithub, sign_in: SignIn
) -> None:
    _org_with(github, 5001)
    sign_in()
    token = client.get("/_test/csrf").json()
    response = client.post(
        "/auth/logout", data={"csrf_token": token}, headers={"Origin": "https://burnbird.test"}
    )
    morsel = _cookie(response, SESSION_COOKIE)
    _host_only_and_locked_down(morsel)
    assert morsel["max-age"] == "0"


# ---------- who is signed in ----------


def _stored_session(session: Session, expires_at: datetime) -> str:
    session.add(GithubUser(id=USER, login="example-person", last_sign_in_at=NOW))
    session.flush()
    value = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            id_sha256=hashlib.sha256(value.encode()).digest(),
            user_id=USER,
            created_at=expires_at - SESSION_LIFETIME,
            expires_at=expires_at,
        )
    )
    session.flush()
    return value


@pytest.mark.parametrize(
    ("now", "status"),
    [(NOW - timedelta(microseconds=1), 200), (NOW, 401), (NOW + timedelta(seconds=1), 401)],
)
def test_a_session_ends_exactly_at_its_expiry(
    now: datetime, status: int, client: TestClient, clock: Clock, session: Session
) -> None:
    client.cookies.set(SESSION_COOKIE, _stored_session(session, NOW))
    clock.fixed = now
    assert client.get("/_test/repositories").status_code == status


@pytest.mark.parametrize("cookie", [None, "", "not-a-session"])
def test_no_cookie_or_an_unknown_one_is_not_signed_in(
    cookie: str | None, client: TestClient, session: Session
) -> None:
    _stored_session(session, NOW + timedelta(hours=1))
    if cookie is not None:
        client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get("/_test/repositories").status_code == 401


def test_the_hash_alone_is_not_a_session(client: TestClient, session: Session) -> None:
    value = _stored_session(session, datetime.now(UTC) + timedelta(hours=1))
    client.cookies.set(SESSION_COOKIE, hashlib.sha256(value.encode()).hexdigest())
    assert client.get("/_test/repositories").status_code == 401


def test_a_revoked_user_is_signed_out_at_once(
    client: TestClient, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    _org_with(github, 5001)
    sign_in()
    assert client.get("/_test/repositories").status_code == 200
    revoke_authorization(session, USER)
    session.flush()
    assert client.get("/_test/repositories").status_code == 401


# ---------- what they may see ----------


OTHER_USER = 7002


def _other_user_sees(session: Session, installation_id: int, repository_id: int) -> None:
    """Another person, whose lists hold what the signed-in user must not see."""
    session.add(GithubUser(id=OTHER_USER, login="other-person", last_sign_in_at=NOW))
    session.flush()
    session.add_all(
        [
            UserInstallation(user_id=OTHER_USER, installation_id=installation_id),
            UserRepository(user_id=OTHER_USER, repository_id=repository_id),
        ]
    )
    session.flush()


def _other_org_stored(session: Session) -> None:
    session.add(Account(id=OTHER_ORG, login="other-org", type="Organization", github_plan="x"))
    session.flush()
    session.add(Installation(id=INSTALLATION[OTHER_ORG], account_id=OTHER_ORG, created_at=NOW))
    session.flush()
    session.add(
        Repository(id=5009, installation_id=INSTALLATION[OTHER_ORG], name="theirs", private=True)
    )
    session.flush()
    _other_user_sees(session, INSTALLATION[OTHER_ORG], 5009)


def test_a_foreign_installation_is_refused(
    client: TestClient, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    _other_org_stored(session)
    _org_with(github, 5001)
    sign_in()
    assert client.get(f"/_test/installations/{INSTALLATION[ORG]}").status_code == 200
    assert client.get(f"/_test/installations/{INSTALLATION[OTHER_ORG]}").status_code == 404
    assert client.get("/_test/installations/9999").status_code == 404
    assert client.get("/_test/repositories/5009").status_code == 404
    assert client.get("/_test/repositories").json() == [5001]


def test_a_private_repository_the_user_cannot_access_is_refused_in_their_own_installation(
    client: TestClient, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    # The installation holds two private repositories; GitHub lists only the one the user can
    # access, but the installation is listed all the same.
    session.add(Account(id=ORG, login="example-org", type="Organization", github_plan="x"))
    session.flush()
    session.add(Installation(id=INSTALLATION[ORG], account_id=ORG, created_at=NOW))
    session.flush()
    session.add(
        Repository(id=5002, installation_id=INSTALLATION[ORG], name="secret", private=True)
    )
    session.flush()
    # A colleague can access it, so it is in the database and in their list.
    _other_user_sees(session, INSTALLATION[ORG], 5002)
    _org_with(github, 5001)
    sign_in()
    assert client.get(f"/_test/installations/{INSTALLATION[ORG]}").status_code == 200
    assert client.get("/_test/repositories/5001").status_code == 200
    assert client.get("/_test/repositories/5002").status_code == 404
    assert client.get("/_test/repositories").json() == [5001]


def test_the_filters_need_a_session(client: TestClient) -> None:
    for path in (
        f"/_test/installations/{INSTALLATION[ORG]}",
        "/_test/repositories/5001",
        "/_test/repositories",
        "/_test/csrf",
    ):
        assert client.get(path).status_code == 401
