"""Sending one email: one POST to the provider, or SMTP to a local Mailpit; one outcome each.

Nothing here retries or waits: ``send`` makes one attempt and returns a ``Result``, and the outbox
decides what happens next. An outcome is always one of these, so a message is sent at most once:

- ``sent``: a 2xx answer.
- ``retry``: the request surely never reached the provider (a connect error, a connect or pool
  timeout) or it refused the whole request (429, 503); safe to send again.
- ``reset``: the daily limit is spent (SM_151, SMI_115); send again after the provider's reset.
- ``config``: our own setup is wrong (401, SERR_156, SERR_157, SM_111); stop sending.
- ``failed``: any other refusal; final.
- ``unknown``: the provider may have taken it (a read or write timeout, a broken answer, a 500,
  502 or 504, as a gateway's error can come after the message was taken); final, never resent.

The provider documents its errors as ``{"data": {"error_code": ...}}`` on its send API page and as
``{"error": {"details": [{"code": ...}]}}`` in its error-code table, so every code-shaped value
under ``code`` or ``error_code`` counts. ``Result.code`` is a short code, never the provider's
text.

The outbox holds its row locked during the POST, so Delete my data waits for a send in progress.
The timeouts bound that wait: 5 s to get a pooled connection, 5 s to connect, 5 s for each write
and 10 s for each read. The request and the answer are each one small JSON body, so the wait is
about 20 s at most in practice; a slow name lookup (not covered by the connect timeout) or an
answer dripped byte by byte could stretch it.
"""

import re
import smtplib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, override

import httpx2

from burnbird.email.config import SmtpSettings, ZohoSettings
from burnbird.email.message import Message, mime, zoho_payload

TIMEOUT: Final = httpx2.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
SMTP_TIMEOUT_SECONDS: Final = 10.0

type Status = Literal["sent", "retry", "reset", "config", "failed", "unknown"]

_CODE: Final = re.compile(r"[A-Z]{1,8}_[0-9]{1,6}")
# The provider's request ID is kept only in this shape, never as arbitrary text.
_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_RESET: Final = frozenset({"SM_151", "SMI_115"})
_CONFIG: Final = frozenset({"SERR_156", "SERR_157", "SM_111"})
_RETRY_STATUSES: Final = frozenset({429, 503})
_MAX_DEPTH: Final = 8


@dataclass(frozen=True, slots=True)
class Result:
    """One attempt's outcome, its short code, and the provider's request ID if it gave one."""

    status: Status
    code: str
    request_id: str | None = None


class Sender(Protocol):
    """Sends one message per call and says how it went."""

    def send(self, message: Message) -> Result:
        """Make one attempt."""
        ...


class ZohoSender:
    """The provider's send API over httpx; the token is sent as ``Zoho-enczapikey``."""

    def __init__(
        self, settings: ZohoSettings, *, transport: httpx2.BaseTransport | None = None
    ) -> None:
        """Keep the URL and an HTTP client that sends the token; no call is made yet."""
        self._url = settings.url
        self._http = httpx2.Client(
            timeout=TIMEOUT,
            transport=transport,
            # The token goes to the configured URL only, never through a proxy or a certificate
            # file some environment variable names.
            trust_env=False,
            headers={
                "Authorization": f"Zoho-enczapikey {settings.key}",
                "Accept": "application/json",
            },
        )

    @override
    def __repr__(self) -> str:
        """Name the sender only: its token never reaches a repr."""
        return "ZohoSender()"

    def send(self, message: Message) -> Result:
        """POST the message once and map the answer to an outcome."""
        try:
            response = self._http.post(self._url, json=zoho_payload(message))
        except httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout:
            return Result("retry", "connect")
        except httpx2.TimeoutException:
            return Result("unknown", "timeout")
        except httpx2.HTTPError:
            # A broken connection or an answer that cannot be decoded: it may have been taken.
            return Result("unknown", "transport")
        return classify(response)

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()


def classify(response: httpx2.Response) -> Result:
    """Map one answer from the provider to its outcome."""
    status = response.status_code
    try:
        body: Any = response.json()
    except ValueError, RecursionError:
        body = None
    if 200 <= status < 300:
        request_id = body.get("request_id") if isinstance(body, dict) else None
        if not (isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)):
            request_id = None
        return Result("sent", f"http:{status}", request_id)
    codes = list(_codes(body))
    if reset := [code for code in codes if code in _RESET]:
        return Result("reset", f"zoho:{reset[0]}")
    if config := [code for code in codes if code in _CONFIG]:
        return Result("config", f"zoho:{config[0]}")
    if status == 401:
        return Result("config", "http:401")
    if status in _RETRY_STATUSES:
        return Result("retry", f"http:{status}")
    if status >= 500:
        return Result("unknown", f"http:{status}")
    return Result("failed", f"zoho:{codes[0]}" if codes else f"http:{status}")


def _codes(body: Any) -> Iterator[str]:
    """Yield every code-shaped value under a ``code`` or ``error_code`` key, in document order.

    The provider's error bodies are a few levels deep; nothing below ``_MAX_DEPTH`` is read, and
    the walk keeps its own stack, so no body can exhaust Python's.
    """
    stack: list[tuple[Any, int]] = [(body, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > _MAX_DEPTH:
            continue
        if isinstance(value, dict):
            items = list(value.items())
        elif isinstance(value, list):
            items = [(None, item) for item in value]
        else:
            continue
        nested: list[tuple[Any, int]] = []
        for key, item in items:
            if key in ("code", "error_code") and isinstance(item, str) and _CODE.fullmatch(item):
                yield item
            else:
                nested.append((item, depth + 1))
        stack.extend(reversed(nested))


class SmtpSender:
    """SMTP to a local Mailpit, for development and tests."""

    def __init__(self, settings: SmtpSettings) -> None:
        """Keep Mailpit's host and port; no connection is made yet."""
        self._host = settings.host
        self._port = settings.port

    def send(self, message: Message) -> Result:
        """Deliver the message once over SMTP."""
        try:
            smtp = smtplib.SMTP(self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS)
        except OSError:
            return Result("retry", "connect")
        try:
            with smtp:
                smtp.send_message(mime(message))
        except smtplib.SMTPResponseException as err:
            return Result("failed", f"smtp:{err.smtp_code}")
        except smtplib.SMTPException, OSError:
            return Result("unknown", "transport")
        return Result("sent", "smtp:250")


def sender(settings: ZohoSettings | SmtpSettings) -> Sender:
    """Return the sender the settings name."""
    if isinstance(settings, ZohoSettings):
        return ZohoSender(settings)
    return SmtpSender(settings)
