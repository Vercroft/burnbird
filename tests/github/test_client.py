"""The App's client against a fake api.github.com, with a fake clock whose sleeps advance it.

Tokens are built from parts at run time, so no token-shaped value is ever committed. The JWT is
checked with joserfc against the public half of the key, independently of how it was signed.
"""

import json
import logging
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from burnbird.github import client
from burnbird.github.client import (
    API_HOST,
    APP,
    GithubAnswerError,
    GithubApp,
    Quota,
    RateLimitedError,
)

CLIENT_ID = "Iv23.example"
INSTALLATION = 4001
START = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
TOKEN_PATH = f"/app/installations/{INSTALLATION}/access_tokens"
# Built from the client's own constant: the leak scan allows only reviewed hosts in a URL literal.
API = f"https://{API_HOST}"

type Answer = httpx2.Response | Exception


def make_token() -> str:
    return "gh" + "s_" + secrets.token_urlsafe(30)


@dataclass
class FakeClock:
    """Wall time and monotonic time that move only when the code sleeps or a test advances them."""

    now: datetime = START
    sleeps: list[float] = field(default_factory=list)

    def wall(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return (self.now - START).total_seconds()

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeGithub:
    """Answers queued per ``METHOD path``; a token request is answered with a new token."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.answers: dict[str, list[Answer]] = {}
        self.requests: list[httpx2.Request] = []
        self.tokens: list[str] = []
        self.token_lifetime = timedelta(hours=1)

    def queue(self, call: str, *answers: Answer) -> None:
        self.answers.setdefault(call, []).extend(answers)

    def calls(self, call: str) -> list[httpx2.Request]:
        return [r for r in self.requests if f"{r.method} {r.url.path}" == call]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        call = f"{request.method} {request.url.path}"
        queued = self.answers.get(call)
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if request.method == "POST" and call.endswith("/access_tokens"):
            token = make_token()
            self.tokens.append(token)
            expires = self.clock.now + self.token_lifetime
            return httpx2.Response(201, json={"token": token, "expires_at": expires.isoformat()})
        return httpx2.Response(200, json={"items": []})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def github(clock: FakeClock) -> FakeGithub:
    return FakeGithub(clock)


@pytest.fixture
def app(rsa_key: RSAKey, github: FakeGithub, clock: FakeClock) -> Iterator[GithubApp]:
    app = GithubApp(
        CLIENT_ID,
        rsa_key,
        transport=httpx2.MockTransport(github.handle),
        clock=clock.wall,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    yield app
    app.close()


def bearer(request: httpx2.Request) -> str:
    scheme, _, value = request.headers["Authorization"].partition(" ")
    assert scheme == "Bearer"
    return value


def get_all(app: GithubApp, path: str = "app/installations", installation: int = APP) -> list[Any]:
    return list(app.pages(path, installation))


def test_the_jwt_is_rs256_issued_by_the_client_id_within_githubs_times(
    app: GithubApp, github: FakeGithub, rsa_key: RSAKey
) -> None:
    get_all(app)
    public = RSAKey.import_key(rsa_key.as_pem(private=False))
    token = jwt.decode(bearer(github.requests[0]), public, algorithms=["RS256"])
    now = int(START.timestamp())
    assert token.header["alg"] == "RS256"
    assert token.claims == {"iat": now - 60, "exp": now + 540, "iss": CLIENT_ID}
    assert token.claims["exp"] - now <= 600


def test_every_call_asks_for_one_rest_version(app: GithubApp, github: FakeGithub) -> None:
    get_all(app)
    get_all(app, "installation/repositories", INSTALLATION)
    for request in github.requests:
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert request.headers["Accept"] == "application/vnd.github+json"


def test_the_token_asks_for_actions_and_metadata_read_and_nothing_else(
    app: GithubApp, github: FakeGithub, rsa_key: RSAKey
) -> None:
    get_all(app, "installation/repositories", INSTALLATION)
    [mint] = github.calls(f"POST {TOKEN_PATH}")
    assert json.loads(mint.content) == {"permissions": {"actions": "read", "metadata": "read"}}
    # The token request is signed with the App JWT, and the list with the token it returned.
    jwt.decode(bearer(mint), RSAKey.import_key(rsa_key.as_pem(private=False)))
    [listing] = github.calls("GET /installation/repositories")
    assert bearer(listing) == github.tokens[0]


def test_a_token_is_reused_until_five_minutes_before_it_expires(
    app: GithubApp, github: FakeGithub, clock: FakeClock
) -> None:
    # The fake's token is minted at START and expires an hour later.
    get_all(app, "installation/repositories", INSTALLATION)
    clock.now = START + timedelta(minutes=55) - timedelta(microseconds=1)
    get_all(app, "installation/repositories", INSTALLATION)
    assert len(github.tokens) == 1
    clock.now = START + timedelta(minutes=55)
    get_all(app, "installation/repositories", INSTALLATION)
    assert len(github.tokens) == 2
    assert bearer(github.calls("GET /installation/repositories")[-1]) == github.tokens[1]


def test_each_installation_has_its_own_token(app: GithubApp, github: FakeGithub) -> None:
    get_all(app, "installation/repositories", INSTALLATION)
    get_all(app, "installation/repositories", INSTALLATION + 1)
    assert len(github.tokens) == 2


def test_a_refused_token_is_made_again_once(app: GithubApp, github: FakeGithub) -> None:
    github.queue("GET /installation/repositories", httpx2.Response(401))
    assert get_all(app, "installation/repositories", INSTALLATION) == [{"items": []}]
    assert len(github.tokens) == 2
    assert bearer(github.calls("GET /installation/repositories")[-1]) == github.tokens[1]


def test_a_token_refused_twice_fails(app: GithubApp, github: FakeGithub) -> None:
    github.queue("GET /installation/repositories", httpx2.Response(401), httpx2.Response(401))
    with pytest.raises(GithubAnswerError, match="answered 401"):
        get_all(app, "installation/repositories", INSTALLATION)
    assert len(github.calls("GET /installation/repositories")) == 2


def test_a_refused_jwt_is_not_retried(app: GithubApp, github: FakeGithub) -> None:
    github.queue("GET /app/installations", httpx2.Response(401))
    with pytest.raises(GithubAnswerError, match="answered 401"):
        get_all(app)
    assert len(github.requests) == 1


def test_a_get_is_retried_on_a_server_error_with_doubling_waits(
    app: GithubApp, github: FakeGithub, clock: FakeClock
) -> None:
    github.queue("GET /app/installations", httpx2.Response(502), httpx2.Response(503))
    assert get_all(app) == [{"items": []}]
    assert len(github.requests) == 3
    assert clock.sleeps == [1, 2]


def test_a_get_is_tried_three_times_at_most(app: GithubApp, github: FakeGithub) -> None:
    github.queue("GET /app/installations", *(httpx2.Response(500) for _ in range(4)))
    with pytest.raises(GithubAnswerError, match="answered 500"):
        get_all(app)
    assert len(github.requests) == 3


def test_a_timeout_is_retried_and_named_by_its_kind(app: GithubApp, github: FakeGithub) -> None:
    timeout = httpx2.ConnectTimeout("timed out")
    github.queue("GET /app/installations", timeout)
    assert get_all(app) == [{"items": []}]
    github.queue("GET /app/installations", timeout, timeout, timeout)
    with pytest.raises(GithubAnswerError, match="failed: ConnectTimeout"):
        get_all(app)


def test_a_client_error_is_not_retried(app: GithubApp, github: FakeGithub) -> None:
    github.queue("GET /app/installations", httpx2.Response(404))
    with pytest.raises(GithubAnswerError, match="answered 404"):
        get_all(app)
    assert len(github.requests) == 1


def test_a_post_is_never_retried(app: GithubApp, github: FakeGithub) -> None:
    github.queue("POST /app/hook/deliveries/9/attempts", httpx2.Response(502))
    with pytest.raises(GithubAnswerError, match="answered 502"):
        app.post("app/hook/deliveries/9/attempts")
    assert len(github.requests) == 1


def test_a_failed_token_request_is_not_retried(app: GithubApp, github: FakeGithub) -> None:
    github.queue(f"POST {TOKEN_PATH}", httpx2.Response(502))
    with pytest.raises(GithubAnswerError, match="access_tokens answered 502"):
        get_all(app, "installation/repositories", INSTALLATION)
    assert len(github.requests) == 1


def test_a_post_answered_2xx_is_returned(app: GithubApp, github: FakeGithub) -> None:
    github.queue("POST /app/hook/deliveries/9/attempts", httpx2.Response(202, json={}))
    assert app.post("app/hook/deliveries/9/attempts").status_code == 202


SECONDARY = {"message": "You have exceeded a secondary rate limit. Please wait a few minutes."}


@pytest.mark.parametrize(
    ("answer", "wait"),
    [
        pytest.param(httpx2.Response(403, json=SECONDARY), timedelta(seconds=60), id="message"),
        pytest.param(
            httpx2.Response(403, headers={"retry-after": "30"}, json=SECONDARY),
            timedelta(seconds=30),
            id="retry-after",
        ),
        pytest.param(httpx2.Response(429), timedelta(seconds=60), id="429"),
        pytest.param(
            httpx2.Response(429, headers={"retry-after": "120"}),
            timedelta(seconds=120),
            id="429 retry-after",
        ),
    ],
)
def test_a_secondary_limit_pauses_the_scope_for_githubs_wait(
    app: GithubApp, github: FakeGithub, answer: httpx2.Response, wait: timedelta
) -> None:
    github.queue("GET /app/installations", answer)
    with pytest.raises(RateLimitedError) as raised:
        get_all(app)
    assert raised.value.resume_at == START + wait
    assert len(github.requests) == 1


def test_a_spent_primary_limit_waits_until_one_second_past_its_reset(
    app: GithubApp, github: FakeGithub
) -> None:
    reset = START + timedelta(minutes=20)
    headers = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-limit": "5000",
        "x-ratelimit-reset": str(int(reset.timestamp())),
    }
    github.queue("GET /app/installations", httpx2.Response(403, headers=headers))
    with pytest.raises(RateLimitedError) as raised:
        get_all(app)
    assert raised.value.resume_at == reset + timedelta(seconds=1)


def test_retry_after_comes_before_the_reset_time(app: GithubApp, github: FakeGithub) -> None:
    reset = START + timedelta(minutes=20)
    headers = {
        "retry-after": "5",
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": str(int(reset.timestamp())),
    }
    github.queue("GET /app/installations", httpx2.Response(429, headers=headers))
    with pytest.raises(RateLimitedError) as raised:
        get_all(app)
    assert raised.value.resume_at == START + timedelta(seconds=5)


def test_a_permission_refusal_is_not_a_rate_limit(app: GithubApp, github: FakeGithub) -> None:
    refusal = {"message": "Resource not accessible by integration"}
    headers = {"x-ratelimit-remaining": "4990", "x-ratelimit-limit": "5000"}
    github.queue("GET /app/installations", httpx2.Response(403, headers=headers, json=refusal))
    with pytest.raises(GithubAnswerError, match="answered 403") as raised:
        get_all(app)
    assert not isinstance(raised.value, RateLimitedError)
    # Nothing is paused: the next call is sent.
    get_all(app)
    assert len(github.requests) == 2


def test_nothing_is_sent_in_a_paused_scope_until_it_resumes(
    app: GithubApp, github: FakeGithub, clock: FakeClock
) -> None:
    github.queue("GET /app/installations", httpx2.Response(403, json=SECONDARY))
    with pytest.raises(RateLimitedError):
        get_all(app)
    clock.advance(59)
    with pytest.raises(RateLimitedError) as raised:
        app.post("app/hook/deliveries/9/attempts")
    assert raised.value.resume_at == START + timedelta(seconds=60)
    assert len(github.requests) == 1
    clock.advance(1)
    get_all(app)
    assert len(github.requests) == 2


def test_a_paused_installation_leaves_the_apps_own_calls_alone(
    app: GithubApp, github: FakeGithub
) -> None:
    github.queue("GET /installation/repositories", httpx2.Response(429))
    with pytest.raises(RateLimitedError):
        get_all(app, "installation/repositories", INSTALLATION)
    get_all(app)
    with pytest.raises(RateLimitedError):
        get_all(app, "installation/repositories", INSTALLATION)
    assert len(github.calls("GET /installation/repositories")) == 1
    assert len(github.calls("GET /app/installations")) == 1


def test_a_rate_limited_token_request_pauses_the_apps_calls(
    app: GithubApp, github: FakeGithub
) -> None:
    github.queue(f"POST {TOKEN_PATH}", httpx2.Response(403, json=SECONDARY))
    with pytest.raises(RateLimitedError):
        get_all(app, "installation/repositories", INSTALLATION)
    with pytest.raises(RateLimitedError):
        get_all(app)
    assert len(github.requests) == 1


def test_calls_are_250_ms_apart_and_posts_one_second(
    app: GithubApp, github: FakeGithub, clock: FakeClock
) -> None:
    get_all(app)
    get_all(app)
    app.post("app/hook/deliveries/1/attempts")
    app.post("app/hook/deliveries/2/attempts")
    assert clock.sleeps == [0.25, 0.25, 1.0]


def test_a_call_after_a_long_gap_does_not_wait(
    app: GithubApp, github: FakeGithub, clock: FakeClock
) -> None:
    get_all(app)
    clock.advance(5)
    get_all(app)
    assert clock.sleeps == []


def page(items: list[int], next_url: str | None = None) -> httpx2.Response:
    headers = {"Link": f'<{next_url}>; rel="next"'} if next_url else {}
    return httpx2.Response(200, headers=headers, json={"items": items})


def test_every_page_is_followed_with_100_a_page(app: GithubApp, github: FakeGithub) -> None:
    second = f"{API}/app/installations?per_page=100&page=2"
    github.queue("GET /app/installations", page([1], second), page([2]))
    assert get_all(app) == [{"items": [1]}, {"items": [2]}]
    assert [str(r.url) for r in github.requests] == [
        f"{API}/app/installations?per_page=100",
        second,
    ]


def test_a_path_with_a_query_keeps_it(app: GithubApp, github: FakeGithub) -> None:
    get_all(app, "repos/example-org/example-app/actions/runs/7/jobs?filter=all", INSTALLATION)
    [listing] = github.calls("GET /repos/example-org/example-app/actions/runs/7/jobs")
    assert listing.url.params["filter"] == "all"
    assert listing.url.params["per_page"] == "100"


def test_more_pages_than_the_cap_fail(
    app: GithubApp, github: FakeGithub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "MAX_PAGES", 2)
    next_url = f"{API}/app/installations?page=n"
    github.queue("GET /app/installations", *(page([n], next_url) for n in range(3)))
    with pytest.raises(GithubAnswerError, match="more than 2 pages"):
        get_all(app)
    assert len(github.requests) == 2


@pytest.mark.parametrize(
    ("scheme", "authority"),
    [
        ("https", "example.test"),
        ("https", f"{API_HOST}:8443"),
        ("https", f"!@{API_HOST}"),
        ("http", API_HOST),
        ("https", f"{API_HOST}.example.test"),
        # A URL parser drops a tab, so the host would read as api.github.com.
        ("https", "api.git\thub.com"),
        ("https", f"{API_HOST}\\@example.test"),
    ],
)
def test_a_next_link_off_the_api_host_fails_closed(
    app: GithubApp, github: FakeGithub, scheme: str, authority: str
) -> None:
    link = f"{scheme}://{authority}/app/installations?page=2"
    github.queue("GET /app/installations", page([1], link))
    with pytest.raises(GithubAnswerError, match=r"linked off api\.github\.com"):
        get_all(app)
    assert len(github.requests) == 1


def test_a_body_that_is_not_json_fails(app: GithubApp, github: FakeGithub) -> None:
    github.queue("GET /app/installations", httpx2.Response(200, content=b"<html>"))
    with pytest.raises(GithubAnswerError, match="not JSON"):
        get_all(app)


def test_the_quota_of_each_scope_is_kept_from_its_last_answer(
    app: GithubApp, github: FakeGithub
) -> None:
    reset = int((START + timedelta(hours=1)).timestamp())
    headers = {
        "x-ratelimit-remaining": "4321",
        "x-ratelimit-limit": "5000",
        "x-ratelimit-reset": str(reset),
    }
    assert app.quota(INSTALLATION) is None
    github.queue("GET /installation/repositories", httpx2.Response(200, headers=headers, json={}))
    get_all(app, "installation/repositories", INSTALLATION)
    assert app.quota(INSTALLATION) == Quota(4321, 5000, datetime.fromtimestamp(reset, UTC))
    assert app.quota(APP) is None


@pytest.mark.parametrize(
    "answer",
    [
        {"token": None, "expires_at": "{planted}"},
        {"token": "{planted}", "expires_at": "not a time"},
        {"expires_at": "{planted}"},
    ],
)
def test_a_token_answer_not_as_documented_fails_without_its_values(
    app: GithubApp, github: FakeGithub, answer: dict[str, Any]
) -> None:
    planted = make_token()
    body = {
        key: value.format(planted=planted) if isinstance(value, str) else value
        for key, value in answer.items()
    }
    github.queue(f"POST {TOKEN_PATH}", httpx2.Response(201, json=body))
    with pytest.raises(GithubAnswerError, match="without a token") as raised:
        get_all(app, "installation/repositories", INSTALLATION)
    assert planted not in f"{raised.value} {raised.value!r}"
    # Raised "from None", so a traceback never prints pydantic's message with the values.
    assert raised.value.__suppress_context__


def test_no_token_or_jwt_reaches_a_log_an_error_or_a_repr(
    app: GithubApp, github: FakeGithub, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    errors: list[Exception] = []
    get_all(app, "installation/repositories", INSTALLATION)
    for answer in (httpx2.Response(401), httpx2.Response(401), httpx2.Response(429)):
        github.queue("GET /installation/repositories", answer)
    for _ in range(2):
        try:
            get_all(app, "installation/repositories", INSTALLATION)
        except GithubAnswerError as err:
            errors.append(err)
    secrets_sent = github.tokens + [bearer(r) for r in github.requests]
    shown = caplog.text + "".join(f"{e} {e!r}" for e in errors) + repr(app)
    assert len(errors) == 2
    for secret in secrets_sent:
        assert secret not in shown


def test_a_token_request_that_times_out_is_sent_once(app: GithubApp, github: FakeGithub) -> None:
    github.queue(f"POST {TOKEN_PATH}", httpx2.ConnectTimeout("timed out"))
    with pytest.raises(GithubAnswerError, match=r"POST .*access_tokens failed: ConnectTimeout"):
        get_all(app, "installation/repositories", INSTALLATION)
    assert len(github.calls(f"POST {TOKEN_PATH}")) == 1
    assert github.calls("GET /installation/repositories") == []


def test_a_post_that_times_out_is_sent_once(app: GithubApp, github: FakeGithub) -> None:
    github.queue("POST /app/hook/deliveries/9/attempts", httpx2.ReadTimeout("timed out"))
    with pytest.raises(GithubAnswerError, match="failed: ReadTimeout"):
        app.post("app/hook/deliveries/9/attempts")
    assert len(github.requests) == 1


def test_a_paused_installation_makes_no_token(
    app: GithubApp, github: FakeGithub, clock: FakeClock
) -> None:
    github.queue(
        "GET /installation/repositories", httpx2.Response(429, headers={"retry-after": "7200"})
    )
    with pytest.raises(RateLimitedError):
        get_all(app, "installation/repositories", INSTALLATION)
    # The token minted at START has expired, and the installation is paused for another 30 minutes.
    clock.now = START + timedelta(minutes=90)
    with pytest.raises(RateLimitedError):
        get_all(app, "installation/repositories", INSTALLATION)
    assert len(github.tokens) == 1
    assert len(github.requests) == 2


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"retry-after": "9" * 30}, id="retry-after too large"),
        pytest.param(
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9" * 30}, id="reset too large"
        ),
        pytest.param({"x-ratelimit-remaining": "0"}, id="no reset"),
    ],
)
def test_a_limit_with_an_unusable_time_waits_the_documented_minute(
    app: GithubApp, github: FakeGithub, headers: dict[str, str]
) -> None:
    github.queue("GET /app/installations", httpx2.Response(403, headers=headers))
    with pytest.raises(RateLimitedError) as raised:
        get_all(app)
    assert raised.value.resume_at == START + timedelta(seconds=60)


@pytest.mark.parametrize("reset", ["9" * 30, "not a time", ""])
def test_an_unusable_quota_header_leaves_a_good_answer_alone(
    app: GithubApp, github: FakeGithub, reset: str
) -> None:
    headers = {
        "x-ratelimit-remaining": "10",
        "x-ratelimit-limit": "5000",
        "x-ratelimit-reset": reset,
    }
    github.queue("GET /app/installations", httpx2.Response(200, headers=headers, json={}))
    assert get_all(app) == [{}]
    assert app.quota(APP) is None
