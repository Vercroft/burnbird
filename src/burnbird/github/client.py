"""The GitHub App's own API client: the App JWT, installation tokens, paging, retries, rate limits.

The worker's calls to GitHub go through ``GithubApp``. Every call has a time limit, calls are
serial and paced, a GET is retried a bounded number of times on a network error or a server
error, and a POST never is. A rate limit is never waited out here: the call raises
``RateLimitedError`` with the time it may resume, and nothing more is sent for that installation
(or for the App's own calls) until then, so the caller puts the work off. GitHub's guidance:
https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api and
https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api

Installation tokens live in this object's memory only, until five minutes before they expire. They
are never stored, logged or put in an exception, and no code assumes their length: GitHub began a
longer stateless format on 2026-04-27.
https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
"""

import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, override

import httpx2
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import AwareDatetime, BaseModel

API_HOST: Final = "api.github.com"
# The REST API version every call asks for:
# https://docs.github.com/en/rest/about-the-rest-api/api-versions
API_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
)
TIMEOUT_SECONDS: Final = 10.0
PER_PAGE: Final = 100
# More pages than this means something is wrong: the list fails rather than returns a part.
MAX_PAGES: Final = 500

# What an installation token may do: Actions read and the Metadata read every App holds, nothing
# else. https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
TOKEN_PERMISSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {"actions": "read", "metadata": "read"}
)
# A token is used until this long before GitHub's expires_at, so it never expires mid-call.
TOKEN_MARGIN: Final = timedelta(minutes=5)
# The App JWT: iat 60 seconds in the past against clock drift, exp under GitHub's 10 minutes.
# https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
JWT_BACKDATE_SECONDS: Final = 60
JWT_LIFETIME_SECONDS: Final = 540

# Serial calls at least this far apart stay under the secondary limit of 900 points a minute
# (240 at most); POSTs at least one second apart, as GitHub asks for many of them.
MIN_INTERVAL_SECONDS: Final = 0.25
MIN_POST_INTERVAL_SECONDS: Final = 1.0
# A GET is tried at most this many times; the waits between tries double from one second.
GET_ATTEMPTS: Final = 3
RETRIED_STATUSES: Final = frozenset({500, 502, 503, 504})
# A secondary limit with neither header: "wait for at least one minute before retrying".
SECONDARY_WAIT: Final = timedelta(minutes=1)
# octokit's plugin-throttling matches this in the message of a 403 or 429.
_SECONDARY: Final = re.compile(r"\bsecondary rate\b", re.IGNORECASE)

# The scope of the App's own calls (signed with the JWT); an installation's scope is its ID.
APP: Final = 0


class GithubAnswerError(Exception):
    """GitHub's API did not answer as documented; the message names the call, never a value.

    ``status`` is the HTTP status of the answer, when there was one.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        """Keep the message and the answer's status."""
        super().__init__(message)
        self.status = status


class RateLimitedError(GithubAnswerError):
    """A rate limit stopped the call; nothing is sent in its scope before ``resume_at``."""

    def __init__(self, call: str, resume_at: datetime) -> None:
        """Name the call and the time it may resume; never a value from the answer."""
        super().__init__(f"{call} is rate limited until {resume_at.isoformat()}")
        self.resume_at = resume_at


class InstallationRefusedError(GithubAnswerError):
    """GitHub refused to make an installation's token (403): it is suspended, or it blocks the App.

    GitHub says a suspended App's "access to the GitHub API or webhook events is blocked for that
    account" (https://docs.github.com/en/rest/apps/apps) without naming the status code, so every
    403 that is not a rate limit counts.
    """


@dataclass(frozen=True, slots=True)
class Quota:
    """The primary rate limit of one scope, from the headers of its last answer."""

    remaining: int
    limit: int
    reset_at: datetime


@dataclass(frozen=True, slots=True)
class _Token:
    value: str = field(repr=False)
    expires_at: datetime


class _TokenAnswer(BaseModel):
    token: str
    expires_at: AwareDatetime


def next_link(response: httpx2.Response, call: str) -> str | None:
    """Return the ``Link: rel="next"`` URL, refusing one that leaves api.github.com.

    The raw text must start with the https origin of ``API_HOST`` and a slash, so no port, user
    part, other host or characters a URL parser drops (a tab or a newline inside the host) can
    send a token elsewhere.
    """
    url = response.links.get("next", {}).get("url")
    if url is None:
        return None
    if not url.startswith(f"https://{API_HOST}/"):
        raise GithubAnswerError(f"{call} linked off {API_HOST}")
    return url


def rate_limit_resume(response: httpx2.Response, now: datetime) -> datetime | None:
    """Return when a rate-limited answer allows the next call; None when it is not a rate limit.

    GitHub's order: ``retry-after`` seconds; else, with ``x-ratelimit-remaining`` 0, the
    ``x-ratelimit-reset`` time; else at least one minute. A secondary limit often comes as a 403
    with neither header, so a 403 counts only when its message names the secondary limit; any
    other 403 is a permission answer.
    """
    if response.status_code not in (httpx2.codes.FORBIDDEN, httpx2.codes.TOO_MANY_REQUESTS):
        return None
    retry_after = response.headers.get("retry-after", "")
    if retry_after.isdecimal():
        try:
            return now + timedelta(seconds=int(retry_after))
        except OverflowError:
            # A wait too large for a date is no wait GitHub documents: the one-minute rule holds.
            return now + SECONDARY_WAIT
    reset = _epoch(response.headers.get("x-ratelimit-reset", ""), offset=1)
    if response.headers.get("x-ratelimit-remaining") == "0" and reset is not None:
        # One second past the reset, as octokit does, so the call lands after it.
        return max(reset, now)
    if response.status_code == httpx2.codes.TOO_MANY_REQUESTS or _SECONDARY.search(response.text):
        return now + SECONDARY_WAIT
    if response.headers.get("x-ratelimit-remaining") == "0":
        return now + SECONDARY_WAIT
    return None


def _epoch(value: str, offset: int = 0) -> datetime | None:
    """Return the UTC time of Unix seconds in a header, or None when it is not a usable time."""
    if not value.isdecimal():
        return None
    try:
        return datetime.fromtimestamp(int(value) + offset, UTC)
    except OverflowError, OSError, ValueError:
        return None


class GithubApp:
    """The App's client: it signs the JWT, keeps installation tokens and makes every call.

    ``clock`` gives the time of day for the JWT, token expiry and rate limits; ``monotonic`` and
    ``sleep`` pace the calls. Tests pass a mock ``transport`` and a fake clock.
    """

    def __init__(
        self,
        client_id: str,
        private_key: RSAKey,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Keep the client ID and key; no call is made until one is asked for."""
        self._client_id = client_id
        self._key = private_key
        self._http = httpx2.Client(
            base_url=f"https://{API_HOST}",
            headers=dict(API_HEADERS),
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._tokens: dict[int, _Token] = {}
        self._paused: dict[int, datetime] = {}
        self._quotas: dict[int, Quota] = {}
        self._last_call: float | None = None
        self._last_post: float | None = None

    @override
    def __repr__(self) -> str:
        """Name the client only: its key and tokens never reach a repr."""
        return f"GithubApp(client_id={self._client_id!r})"

    def close(self) -> None:
        """Close the HTTP connections."""
        self._http.close()

    def pages(self, path: str, installation: int = APP) -> Iterator[Any]:
        """Yield each page of a list, ``per_page`` 100, following next links on api.github.com.

        ``installation`` is the installation whose token signs the calls, or ``APP`` for the
        App's own calls with the JWT. More than ``MAX_PAGES`` pages raise.
        """
        separator = "&" if "?" in path else "?"
        url: str | None = f"/{path}{separator}per_page={PER_PAGE}"
        call = f"GET /{path}"
        taken = 0
        while url is not None:
            if taken == MAX_PAGES:
                raise GithubAnswerError(f"{call} has more than {MAX_PAGES} pages")
            response = self._get(url, call, installation)
            taken += 1
            try:
                page = response.json()
            except ValueError:
                raise GithubAnswerError(f"{call} answered a body that is not JSON") from None
            yield page
            url = next_link(response, call)

    def post(self, path: str) -> httpx2.Response:
        """POST as the App, with the JWT; never retried. Any answer but 2XX raises."""
        call = f"POST /{path}"
        response = self._send("POST", f"/{path}", call, APP, self._jwt())
        if not response.is_success:
            raise GithubAnswerError(
                f"{call} answered {response.status_code}", response.status_code
            )
        return response

    def quota(self, installation: int = APP) -> Quota | None:
        """Return the primary rate limit of a scope from its last answer; None before any."""
        return self._quotas.get(installation)

    def _get(self, url: str, call: str, installation: int) -> httpx2.Response:
        """GET with bounded retries; a renewed token after a 401 is not counted as an attempt.

        The authorization is made outside the retry, so a failed token request (a POST) is never
        sent again here, and a paused scope makes no token.
        """
        attempt = 1
        renewed = False
        while True:
            self._check_paused(installation, call)
            authorization = self._auth(installation)
            try:
                response = self._send("GET", url, call, installation, authorization)
            except httpx2.TransportError as err:
                if attempt == GET_ATTEMPTS:
                    raise GithubAnswerError(f"{call} failed: {type(err).__name__}") from None
            else:
                if response.is_success:
                    return response
                if (
                    response.status_code == httpx2.codes.UNAUTHORIZED
                    and installation != APP
                    and not renewed
                ):
                    # A token GitHub refuses (revoked, or not yet known everywhere just after it
                    # was made) is dropped and made again, once.
                    self._tokens.pop(installation, None)
                    renewed = True
                    continue
                if response.status_code not in RETRIED_STATUSES or attempt == GET_ATTEMPTS:
                    raise GithubAnswerError(
                        f"{call} answered {response.status_code}", response.status_code
                    )
            self._sleep(2 ** (attempt - 1))
            attempt += 1

    def _send(
        self,
        method: str,
        url: str,
        call: str,
        scope: int,
        authorization: str,
        body: Mapping[str, Any] | None = None,
    ) -> httpx2.Response:
        """Send one call in ``scope``, unless the scope is paused; pause it on a rate limit.

        A GET's network error is left to ``_get``, which retries it; a POST's ends the call.
        """
        self._check_paused(scope, call)
        self._pace(method)
        try:
            response = self._http.request(
                method, url, headers={"Authorization": f"Bearer {authorization}"}, json=body
            )
        except httpx2.TransportError as err:
            if method == "GET":
                raise
            raise GithubAnswerError(f"{call} failed: {type(err).__name__}") from None
        self._record_quota(scope, response)
        resume_at = rate_limit_resume(response, self._clock())
        if resume_at is not None:
            self._paused[scope] = resume_at
            raise RateLimitedError(call, resume_at)
        return response

    def _check_paused(self, scope: int, call: str) -> None:
        paused = self._paused.get(scope)
        if paused is not None and self._clock() < paused:
            raise RateLimitedError(call, paused)

    def _pace(self, method: str) -> None:
        now = self._monotonic()
        waits = [0.0]
        if self._last_call is not None:
            waits.append(self._last_call + MIN_INTERVAL_SECONDS - now)
        if method == "POST" and self._last_post is not None:
            waits.append(self._last_post + MIN_POST_INTERVAL_SECONDS - now)
        if (wait := max(waits)) > 0:
            self._sleep(wait)
        self._last_call = self._monotonic()
        if method == "POST":
            self._last_post = self._last_call

    def _record_quota(self, scope: int, response: httpx2.Response) -> None:
        values = [
            response.headers.get(f"x-ratelimit-{name}", "")
            for name in ("remaining", "limit", "reset")
        ]
        reset_at = _epoch(values[2])
        if values[0].isdecimal() and values[1].isdecimal() and reset_at is not None:
            self._quotas[scope] = Quota(int(values[0]), int(values[1]), reset_at)

    def _auth(self, installation: int) -> str:
        return self._jwt() if installation == APP else self._token(installation)

    def _jwt(self) -> str:
        now = int(self._clock().timestamp())
        claims = {
            "iat": now - JWT_BACKDATE_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": self._client_id,
        }
        return jwt.encode({"alg": "RS256"}, claims, self._key)

    def _token(self, installation: int) -> str:
        token = self._tokens.get(installation)
        if token is not None and token.expires_at - self._clock() > TOKEN_MARGIN:
            return token.value
        path = f"/app/installations/{installation}/access_tokens"
        call = f"POST {path}"
        # Signed with the JWT, so it is one of the App's own calls; never retried.
        response = self._send(
            "POST", path, call, APP, self._jwt(), {"permissions": dict(TOKEN_PERMISSIONS)}
        )
        if response.status_code == httpx2.codes.FORBIDDEN:
            raise InstallationRefusedError(f"{call} answered 403", 403)
        if response.status_code != httpx2.codes.CREATED:
            raise GithubAnswerError(
                f"{call} answered {response.status_code}", response.status_code
            )
        try:
            answer = _TokenAnswer.model_validate(response.json())
        except ValueError:
            # Pydantic's message would repeat the values, the token among them.
            raise GithubAnswerError(f"{call} answered without a token") from None
        self._tokens[installation] = _Token(answer.token, answer.expires_at)
        return answer.token
