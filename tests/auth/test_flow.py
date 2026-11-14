import hashlib
import logging
import secrets
import time
import types
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx2
import itsdangerous.timed
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from burnbird import logs
from burnbird.auth.config import AuthSettings
from burnbird.auth.dependencies import AuthState
from burnbird.auth.service import MAX_PAGES, OAUTH_COOKIE, OAUTH_COOKIE_SECONDS, SESSION_COOKIE
from burnbird.models import (
    Account,
    Base,
    GithubUser,
    Installation,
    Repository,
    UserInstallation,
    UserRepository,
    UserSession,
)
from burnbird.web import create_app
from tests.auth.conftest import (
    INSTALLATION,
    ORG,
    OTHER_ORG,
    USER,
    FakeGithub,
    installation_item,
    oauth_data,
    repository_item,
    s256,
)

type SignIn = Callable[[], httpx2.Response]

UNFINISHED_LOGINS = 20
BROWSER_COOKIE_LIMIT = 4096
# Authlib keeps each login's state under this key in the OAuth cookie.
STATE_KEY_PREFIX = "_state_github_"


def _lists(session: Session) -> tuple[set[int], set[int]]:
    session.expire_all()
    installations = set(
        session.scalars(
            select(UserInstallation.installation_id).where(UserInstallation.user_id == USER)
        )
    )
    repositories = set(
        session.scalars(select(UserRepository.repository_id).where(UserRepository.user_id == USER))
    )
    return installations, repositories


def _sessions(session: Session) -> list[UserSession]:
    session.expire_all()
    return list(session.scalars(select(UserSession)))


def _org_with(github: FakeGithub, *repositories: int) -> None:
    github.installations = [installation_item(INSTALLATION[ORG], ORG)]
    github.repositories = {INSTALLATION[ORG]: [repository_item(r) for r in repositories]}


def _refused_without_exchange(
    response: httpx2.Response, github: FakeGithub, session: Session, status: int = 400
) -> None:
    assert response.status_code == status
    assert github.exchanges() == []
    assert SESSION_COOKIE not in response.cookies
    assert _sessions(session) == []


# ---------- success ----------


def test_sign_in_stores_the_user_their_lists_and_a_hashed_session(
    client: TestClient, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    _org_with(github, 5001, 5002)
    response = sign_in()
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert response.headers["cache-control"] == "no-store"
    assert _lists(session) == ({INSTALLATION[ORG]}, {5001, 5002})
    user = session.get(GithubUser, USER)
    assert user is not None
    assert user.login == "example-person"
    # The installation and its account were not stored yet: sign-in adds them.
    account = session.get(Account, ORG)
    assert account is not None
    assert account.type == "Organization"
    assert session.get(Installation, INSTALLATION[ORG]) is not None
    repository = session.get(Repository, 5002)
    assert repository is not None
    assert (repository.installation_id, repository.private) == (INSTALLATION[ORG], True)
    # Only the SHA-256 of the cookie's value is stored.
    [stored] = _sessions(session)
    cookie = client.cookies[SESSION_COOKIE]
    assert stored.id_sha256 == hashlib.sha256(cookie.encode()).digest()
    assert stored.expires_at - stored.created_at == (
        datetime(2026, 1, 1, 8, tzinfo=UTC) - datetime(2026, 1, 1, tzinfo=UTC)
    )


def test_the_api_calls_ask_for_one_rest_version_with_the_token(
    github: FakeGithub, sign_in: SignIn
) -> None:
    _org_with(github, 5001)
    assert sign_in().status_code == 303
    api = [r for r in github.requests if r.url.host == "api.github.com"]
    assert [r.url.path for r in api] == [
        "/user",
        "/user/installations",
        f"/user/installations/{INSTALLATION[ORG]}/repositories",
    ]
    for request in api:
        assert request.headers["x-github-api-version"] == "2026-03-10"
        assert request.headers["accept"] == "application/vnd.github+json"


def test_every_page_of_every_list_is_stored(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    github.page_size = 2
    github.installations = [installation_item(4000 + n, 3000 + n) for n in range(1, 6)]
    github.repositories = {
        4000 + n: [repository_item(5000 + n * 10 + r) for r in range(3)] for n in range(1, 6)
    }
    assert sign_in().status_code == 303
    assert _lists(session) == (
        {4001, 4002, 4003, 4004, 4005},
        {5000 + n * 10 + r for n in range(1, 6) for r in range(3)},
    )


def test_a_later_sign_in_replaces_both_lists_and_the_session(
    client: TestClient, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    github.installations = [
        installation_item(INSTALLATION[ORG], ORG),
        installation_item(INSTALLATION[OTHER_ORG], OTHER_ORG),
    ]
    github.repositories = {
        INSTALLATION[ORG]: [repository_item(5001), repository_item(5002)],
        INSTALLATION[OTHER_ORG]: [repository_item(5003)],
    }
    assert sign_in().status_code == 303
    first = client.cookies[SESSION_COOKIE]
    _org_with(github, 5001)
    assert sign_in().status_code == 303
    assert _lists(session) == ({INSTALLATION[ORG]}, {5001})
    # The session is renewed: the old row is gone, and only the new cookie's hash is stored.
    second = client.cookies[SESSION_COOKIE]
    assert second != first
    assert [s.id_sha256 for s in _sessions(session)] == [hashlib.sha256(second.encode()).digest()]


def test_an_installation_burnbird_does_not_handle_is_skipped(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    github.installations = [
        installation_item(INSTALLATION[ORG], ORG),
        installation_item(4009, 3009, kind="Enterprise"),
    ]
    github.repositories = {
        INSTALLATION[ORG]: [repository_item(5001)],
        4009: [repository_item(5009)],
    }
    assert sign_in().status_code == 303
    assert _lists(session) == ({INSTALLATION[ORG]}, {5001})
    assert session.get(Installation, 4009) is None


# ---------- state and PKCE ----------


def test_the_authorize_redirect_carries_state_and_an_s256_challenge_of_the_kept_verifier(
    client: TestClient, github: FakeGithub, auth_settings: AuthSettings, sign_in: SignIn
) -> None:
    login = client.get("/auth/login")
    assert login.status_code == 302
    assert login.headers["cache-control"] == "no-store"
    location = urlsplit(login.headers["location"])
    assert (location.scheme, location.netloc, location.path) == (
        "https",
        "github.com",
        "/login/oauth/authorize",
    )
    query = {key: values[0] for key, values in parse_qs(location.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == auth_settings.client_id
    assert query["redirect_uri"] == auth_settings.callback_url
    assert query["code_challenge_method"] == "S256"
    kept = oauth_data(client)[f"_state_github_{query['state']}"]["data"]
    verifier = kept["code_verifier"]
    # RFC 7636: 43 to 128 characters.
    assert 43 <= len(verifier) <= 128
    assert query["code_challenge"] == s256(verifier)
    # The exchange sends that verifier, with the code GitHub bound to its challenge.
    code, state = github.authorize(login.headers["location"])
    assert client.get("/auth/callback", params={"code": code, "state": state}).status_code == 303
    [exchange] = github.exchanges()
    assert parse_qs(exchange.content.decode())["code_verifier"] == [verifier]


def test_a_state_that_is_not_in_the_cookie_is_refused_before_the_exchange(
    client: TestClient, github: FakeGithub, session: Session
) -> None:
    code, _ = github.authorize(client.get("/auth/login").headers["location"])
    response = client.get("/auth/callback", params={"code": code, "state": secrets.token_hex(15)})
    _refused_without_exchange(response, github, session)


def test_a_callback_without_the_oauth_cookie_is_refused(
    client: TestClient, github: FakeGithub, session: Session
) -> None:
    code, state = github.authorize(client.get("/auth/login").headers["location"])
    client.cookies.delete(OAUTH_COOKIE)
    response = client.get("/auth/callback", params={"code": code, "state": state})
    _refused_without_exchange(response, github, session)


def test_a_tampered_oauth_cookie_is_refused(
    client: TestClient, github: FakeGithub, session: Session
) -> None:
    code, state = github.authorize(client.get("/auth/login").headers["location"])
    # Change the first character of the signed data: the signature no longer matches it.
    value = client.cookies[OAUTH_COOKIE]
    client.cookies.delete(OAUTH_COOKIE)
    client.cookies.set(OAUTH_COOKIE, ("B" if value[0] == "A" else "A") + value[1:])
    response = client.get("/auth/callback", params={"code": code, "state": state})
    _refused_without_exchange(response, github, session)


def _unfinished_logins(client: TestClient, github: FakeGithub) -> list[tuple[str, str]]:
    """Start sign-in again and again without a callback; return each login's code and state.

    Every login keeps only its own state, so the OAuth cookie stays under the browser's 4096-byte
    limit for one cookie (RFC 6265 section 6.1); a cookie over it is dropped, and every sign-in
    after that would fail. Authlib issue 622.
    """
    started = []
    for _ in range(UNFINISHED_LOGINS):
        login = client.get("/auth/login")
        [cookie] = [
            header.split(";", 1)[0]
            for header in login.headers.get_list("set-cookie")
            if header.startswith(f"{OAUTH_COOKIE}=")
        ]
        assert len(cookie.encode()) < BROWSER_COOKIE_LIMIT
        assert [key for key in oauth_data(client) if key.startswith(STATE_KEY_PREFIX)] == [
            f"{STATE_KEY_PREFIX}{parse_qs(urlsplit(login.headers['location']).query)['state'][0]}"
        ]
        started.append(github.authorize(login.headers["location"]))
    return started


def test_many_unfinished_logins_keep_the_cookie_small_and_the_last_one_signs_in(
    client: TestClient, github: FakeGithub, session: Session
) -> None:
    *_, (code, state) = _unfinished_logins(client, github)
    response = client.get("/auth/callback", params={"code": code, "state": state})
    assert response.status_code == 303
    assert len(_sessions(session)) == 1


def test_the_state_of_an_earlier_login_is_refused(
    client: TestClient, github: FakeGithub, session: Session
) -> None:
    (code, state), *_ = _unfinished_logins(client, github)
    response = client.get("/auth/callback", params={"code": code, "state": state})
    _refused_without_exchange(response, github, session)


def test_the_github_client_has_no_cache(
    sessions: sessionmaker[Session], auth_settings: AuthSettings
) -> None:
    # With a cache, Authlib keeps the state there and only a marker in the cookie; the state
    # advisories CVE-2025-68158 and CVE-2026-41425 need one. Without it, the state lives only in
    # the signed cookie of the browser that started the sign-in.
    state = create_app(secrets.token_bytes(32), sessions, auth_settings).state.auth
    assert isinstance(state, AuthState)
    assert state.github.framework.cache is None


def test_an_oauth_cookie_older_than_its_limit_is_refused(
    client: TestClient, github: FakeGithub, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, state = github.authorize(client.get("/auth/login").headers["location"])
    later = time.time() + OAUTH_COOKIE_SECONDS + 2
    monkeypatch.setattr(itsdangerous.timed, "time", types.SimpleNamespace(time=lambda: later))
    response = client.get("/auth/callback", params={"code": code, "state": state})
    _refused_without_exchange(response, github, session)


def test_an_injected_code_fails_the_pkce_check(
    client: TestClient, app: FastAPI, github: FakeGithub, session: Session
) -> None:
    # An attacker starts their own sign-in and gets a code bound to their own challenge, then
    # brings it to the victim's callback with the victim's state: the verifier does not match.
    with TestClient(app, base_url="https://burnbird.test", follow_redirects=False) as attacker:
        attackers_code, _ = github.authorize(attacker.get("/auth/login").headers["location"])
    _, state = github.authorize(client.get("/auth/login").headers["location"])
    response = client.get("/auth/callback", params={"code": attackers_code, "state": state})
    assert response.status_code == 400
    assert len(github.exchanges()) == 1
    assert _sessions(session) == []


def test_a_used_code_is_refused(
    client: TestClient, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    _org_with(github, 5001)
    login = client.get("/auth/login")
    code, state = github.authorize(login.headers["location"])
    assert client.get("/auth/callback", params={"code": code, "state": state}).status_code == 303
    _, second_state = github.authorize(client.get("/auth/login").headers["location"])
    again = client.get("/auth/callback", params={"code": code, "state": second_state})
    assert again.status_code == 400
    assert len(_sessions(session)) == 1


def test_a_callback_without_state_starts_a_new_flow_and_never_exchanges(
    client: TestClient, github: FakeGithub, session: Session
) -> None:
    # GitHub's own redirect after an install brings a code without state or PKCE.
    response = client.get("/auth/callback", params={"code": "abc123"})
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"
    _refused_without_exchange(response, github, session, status=303)


@pytest.mark.parametrize(
    ("error", "logged"),
    [("access_denied", "access_denied"), ("<script>alert(1)</script>", "other")],
)
def test_an_error_from_github_is_refused_and_only_a_known_shape_logged(
    error: str,
    logged: str,
    client: TestClient,
    github: FakeGithub,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client.get("/auth/login")
    response = client.get("/auth/callback", params={"error": error, "state": "x"})
    _refused_without_exchange(response, github, session)
    assert f"GitHub returned {logged}" in caplog.text
    assert "<script>" not in caplog.text


def test_every_answer_clears_the_oauth_cookie(client: TestClient, github: FakeGithub) -> None:
    client.get("/auth/login")
    response = client.get("/auth/callback", params={"state": "x", "code": "y"})
    assert response.status_code == 400
    assert OAUTH_COOKIE not in client.cookies


# ---------- GitHub failing ----------


def test_a_timed_out_exchange_is_not_retried(
    client: TestClient, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    github.timeouts.add("/login/oauth/access_token")
    assert sign_in().status_code == 502
    assert len(github.exchanges()) == 1
    assert _sessions(session) == []


def test_a_failing_token_endpoint_is_a_bad_gateway(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    github.failures["/login/oauth/access_token"] = 503
    assert sign_in().status_code == 502
    assert _sessions(session) == []


def test_wrong_client_credentials_are_our_error(
    github: FakeGithub, sign_in: SignIn, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    github.client_secret = secrets.token_hex(20)
    assert sign_in().status_code == 500
    assert "incorrect_client_credentials" in caplog.text
    assert _sessions(session) == []


@pytest.mark.parametrize(
    "path",
    [
        "/user",
        "/user/installations",
        f"/user/installations/{INSTALLATION[ORG]}/repositories",
    ],
)
def test_a_failing_list_stores_nothing_and_keeps_the_old_lists(
    path: str, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    _org_with(github, 5001, 5002)
    assert sign_in().status_code == 303
    before = (_lists(session), [s.id_sha256 for s in _sessions(session)])
    _org_with(github, 5001)
    github.failures[path] = 500
    assert sign_in().status_code == 502
    assert (_lists(session), [s.id_sha256 for s in _sessions(session)]) == before


def test_a_failure_on_a_later_page_stores_nothing(
    github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    github.page_size = 1
    github.installations = [installation_item(4000 + n, 3000 + n) for n in range(1, 4)]
    github.timeouts.add("/user/installations/4003/repositories")
    assert sign_in().status_code == 502
    assert _lists(session) == (set(), set())
    assert session.get(GithubUser, USER) is None


def test_too_many_pages_fail_closed(github: FakeGithub, sign_in: SignIn, session: Session) -> None:
    github.page_size = 1
    github.installations = [installation_item(4000 + n, 3000 + n) for n in range(MAX_PAGES + 1)]
    assert sign_in().status_code == 502
    assert _sessions(session) == []


@pytest.mark.parametrize(
    "link_host",
    ["api.github.com.example.com", "api.github.com:8443", "!@api.github.com"],
)
def test_a_next_link_off_the_api_host_fails_closed(
    link_host: str, github: FakeGithub, sign_in: SignIn, session: Session
) -> None:
    github.page_size = 1
    github.link_host = link_host
    github.installations = [installation_item(4001, 3001), installation_item(4002, 3002)]
    assert sign_in().status_code == 502
    assert all(r.url.host in {"github.com", "api.github.com"} for r in github.requests)
    assert _sessions(session) == []


def test_a_failed_store_clears_the_oauth_cookie_and_stores_nothing(
    client: TestClient,
    github: FakeGithub,
    sign_in: SignIn,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object) -> str:
        raise OperationalError("INSERT", {}, Exception("the database is gone"))

    monkeypatch.setattr("burnbird.auth.router.store_sign_in", broken)
    _org_with(github, 5001)
    response = sign_in()
    assert response.status_code == 500
    assert OAUTH_COOKIE not in client.cookies
    assert SESSION_COOKIE not in response.cookies
    assert _sessions(session) == []


# ---------- the token is never logged or stored ----------


@pytest.mark.usefixtures("restore_logging")
def test_no_token_verifier_or_code_reaches_a_log_or_a_row(
    client: TestClient,
    github: FakeGithub,
    auth_settings: AuthSettings,
    session: Session,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The command's own set-up at DEBUG, with pytest's handler kept to read the raw records too.
    logs.configure(logging.DEBUG)
    logging.getLogger().addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG)
    _org_with(github, 5001)
    login = client.get("/auth/login")
    verifier = next(iter(oauth_data(client).values()))["data"]["code_verifier"]
    code, state = github.authorize(login.headers["location"])
    assert client.get("/auth/callback", params={"code": code, "state": state}).status_code == 303
    secrets_seen = [
        github.token,
        github.refresh_token,
        verifier,
        code,
        auth_settings.client_secret,
        client.cookies[SESSION_COOKIE],
    ]
    written = capsys.readouterr().err
    assert "sign-in: user 7001" in caplog.text
    assert "sign-in: user 7001" in written
    # The raw records hold none of them. The code is the exception: the test's own client logs
    # the callback URL it requests, as a server's access log would, and the formatter removes it.
    for value in secrets_seen:
        assert value not in written
        if value != code:
            assert value not in caplog.text
    assert code in caplog.text
    session.flush()
    for table in Base.metadata.sorted_tables:
        for row in session.execute(select(table)):
            stored = repr(tuple(row))
            assert not any(value in stored for value in secrets_seen), table.name
