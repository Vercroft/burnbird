"""Sending: one POST to the provider, each answer mapped to one outcome; never a real request.

Every provider answer here comes from an httpx MockTransport, in the shapes the provider documents:
``{"data": {"error_code": ...}}`` on its send API page, and ``{"error": {"details": [{"code":
...}]}}`` in its error-code table.
"""

import uuid
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from burnbird.email.config import SmtpSettings, ZohoSettings
from burnbird.email.message import Message
from burnbird.email.sender import TIMEOUT, Result, SmtpSender, ZohoSender

URL = "https://cpaas.example.invalid/v1.1/email"
TOKEN = "Example" + "SendToken" + "0123456789"


def _message() -> Message:
    return Message(
        reference=uuid.UUID(int=9),
        to="person" + "@" + "example.invalid",
        subject="Example",
        text="Example text",
        html="<p>Example</p>",
        unsubscribe_url=None,
    )


def _send(
    answer: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[Result, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return answer(request)

    sender = ZohoSender(ZohoSettings(url=URL, key=TOKEN), transport=httpx2.MockTransport(handle))
    try:
        return sender.send(_message()), requests
    finally:
        sender.close()


def _answer(status: int, body: Any = None) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda _: httpx2.Response(status, json=body if body is not None else {})


def _raise(error: type[httpx2.RequestError]) -> Callable[[httpx2.Request], httpx2.Response]:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise error("example", request=request)

    return handle


def _code(code: str) -> dict[str, Any]:
    return {"data": {"error_code": code, "message": "example"}, "message": "error"}


def _detail(code: str) -> dict[str, Any]:
    return {"error": {"code": "TM_3601", "details": [{"code": code, "message": "example"}]}}


def test_one_post_with_the_token_and_the_payload() -> None:
    result, [request] = _send(_answer(200, {"message": "OK", "request_id": "req-example"}))
    assert result == Result("sent", "http:200", "req-example")
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.headers["Authorization"] == f"Zoho-enczapikey {TOKEN}"
    assert b'"client_reference":"00000000-0000-0000-0000-000000000009"' in request.content


def test_the_timeouts_bound_how_long_a_send_holds_its_row() -> None:
    # Delete my data waits for a send in progress: at most connect 5 s + write 5 s + read 10 s.
    assert httpx2.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0) == TIMEOUT


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (_answer(201, {}), Result("sent", "http:201", None)),
        (_answer(429), Result("retry", "http:429")),
        (_answer(503), Result("retry", "http:503")),
        (_raise(httpx2.ConnectError), Result("retry", "connect")),
        (_raise(httpx2.ConnectTimeout), Result("retry", "connect")),
        (_raise(httpx2.PoolTimeout), Result("retry", "connect")),
        (_answer(400, _code("SM_151")), Result("reset", "zoho:SM_151")),
        (_answer(400, _detail("SMI_115")), Result("reset", "zoho:SMI_115")),
        (_answer(401), Result("config", "http:401")),
        (_answer(400, _detail("SERR_156")), Result("config", "zoho:SERR_156")),
        (_answer(400, _code("SERR_157")), Result("config", "zoho:SERR_157")),
        (_answer(400, _code("SM_111")), Result("config", "zoho:SM_111")),
        (_answer(400, _code("TM_3004")), Result("failed", "zoho:TM_3004")),
        (_answer(422), Result("failed", "http:422")),
        (_answer(302), Result("failed", "http:302")),
        # The provider may have taken the message: never sent again (at most once).
        (_answer(500), Result("unknown", "http:500")),
        (_answer(502), Result("unknown", "http:502")),
        (_answer(504), Result("unknown", "http:504")),
        (_raise(httpx2.ReadTimeout), Result("unknown", "timeout")),
        (_raise(httpx2.WriteTimeout), Result("unknown", "timeout")),
        (_raise(httpx2.RemoteProtocolError), Result("unknown", "transport")),
    ],
)
def test_every_answer_has_one_outcome(
    answer: Callable[[httpx2.Request], httpx2.Response], expected: Result
) -> None:
    result, _ = _send(answer)
    assert result == expected


def test_a_code_that_is_not_shaped_like_one_is_never_kept() -> None:
    # The outcome holds a short code only, never text the provider sent.
    result, _ = _send(_answer(400, _code("Invalid <b>request</b> for a.person@example.invalid")))
    assert result == Result("failed", "http:400")


def test_a_body_that_is_not_json_still_has_an_outcome() -> None:
    result, _ = _send(lambda _: httpx2.Response(400, text="not json"))
    assert result == Result("failed", "http:400")


def test_mailpit_refusing_the_connection_is_retried() -> None:
    # Nothing listens on port 9 of loopback in the test environment.
    assert SmtpSender(SmtpSettings(host="127.0.0.1", port=9)).send(_message()) == Result(
        "retry", "connect"
    )


def test_a_2xx_whose_body_cannot_be_decoded_is_unknown_not_an_exception() -> None:
    # The provider took it; the body is not the gzip it claims to be.
    result, _ = _send(
        lambda _: httpx2.Response(200, headers={"Content-Encoding": "gzip"}, content=b"not gzip")
    )
    assert result == Result("unknown", "transport")


def test_a_body_nested_too_deep_to_parse_still_has_an_outcome() -> None:
    depth = 100_000
    result, _ = _send(lambda _: httpx2.Response(400, text="[" * depth + "]" * depth))
    assert result == Result("failed", "http:400")


@pytest.mark.parametrize("request_id", ["x" * 500, "req <b>bold</b>", ""])
def test_a_request_id_that_is_not_shaped_like_one_is_not_kept(request_id: str) -> None:
    result, _ = _send(_answer(200, {"message": "OK", "request_id": request_id}))
    assert result == Result("sent", "http:200", None)
