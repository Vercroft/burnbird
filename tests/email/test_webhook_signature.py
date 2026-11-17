"""The provider's webhook signature: HMAC-SHA256 of the raw body, base64 then URL-encoded.

Every key, body and time is made at run time; nothing comes from a real request.
"""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

from burnbird.email.webhook_signature import WINDOW, is_valid

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def sign(key: bytes, body: bytes, at: datetime = NOW, parts: dict[str, str] | None = None) -> str:
    """A ``producer-signature`` header as the provider's page shows it."""
    digest = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()
    fields = {
        "ts": str(int(at.timestamp() * 1000)),
        "s": quote(digest, safe=""),
        "s-algorithm": "HmacSHA256",
    } | (parts or {})
    return ";".join(f"{name}={value}" for name, value in fields.items())


@pytest.fixture
def key() -> bytes:
    return secrets.token_urlsafe(48).encode()


@pytest.fixture
def body() -> bytes:
    return (
        b'{"event_name":["hardbounce"],"webhook_request_id":"'
        + secrets.token_hex(8).encode()
        + b'"}'
    )


def test_a_signature_over_the_raw_body_passes(key: bytes, body: bytes) -> None:
    assert is_valid(key, body, sign(key, body), NOW)


def test_base64_padding_arrives_url_encoded() -> None:
    # A 32-byte digest is 44 base64 characters ending in one "=", which arrives as %3D.
    key, body = secrets.token_bytes(32), b"{}"
    header = sign(key, body)
    assert header.split(";")[1].endswith("%3D")
    assert is_valid(key, body, header, NOW)


def test_the_parts_may_come_in_any_order_with_spaces_around_names(key: bytes, body: bytes) -> None:
    parts = dict(part.split("=", 1) for part in sign(key, body).split(";"))
    header = f" s-algorithm={parts['s-algorithm']}; s={parts['s']} ;ts={parts['ts']}"
    assert is_valid(key, body, header, NOW)


def _changed_body(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body + b" ", sign(key, body)


def _other_key(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body, sign(secrets.token_bytes(32), body)


def _not_url_encoded(key: bytes, body: bytes) -> tuple[bytes, str]:
    digest = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()
    return body, sign(key, body, parts={"s": digest.replace("=", "%") + "%zz"})


def _not_base64(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body, sign(key, body, parts={"s": "not*base64"})


def _other_algorithm(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body, sign(key, body, parts={"s-algorithm": "HmacSHA1"})


def _missing_part(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body, ";".join(p for p in sign(key, body).split(";") if not p.startswith("ts="))


def _repeated_part(key: bytes, body: bytes) -> tuple[bytes, str]:
    # A wrong signature first and the right one after: a parser that kept either one alone, the
    # first or the last, would decide differently; a repeat is refused instead.
    return body, "s=" + quote(base64.b64encode(b"x" * 32).decode(), safe="") + ";" + sign(
        key, body
    )


def _ts_not_integer(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body, sign(key, body, parts={"ts": "1.5e12"})


def _too_old(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body, sign(key, body, at=NOW - WINDOW - timedelta(milliseconds=1))


def _too_far_ahead(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body, sign(key, body, at=NOW + WINDOW + timedelta(milliseconds=1))


def _not_ascii(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body, sign(key, body, parts={"s": "\u00e9"})


def _empty(key: bytes, body: bytes) -> tuple[bytes, str]:
    return body, ""


@pytest.mark.parametrize(
    "make",
    [
        _changed_body,
        _other_key,
        _not_url_encoded,
        _not_base64,
        _other_algorithm,
        _missing_part,
        _repeated_part,
        _ts_not_integer,
        _too_old,
        _too_far_ahead,
        _not_ascii,
        _empty,
    ],
    ids=lambda make: make.__name__.strip("_").replace("_", " "),
)
def test_a_bad_signature_is_refused_never_raised(
    key: bytes, body: bytes, make: Callable[[bytes, bytes], tuple[bytes, str]]
) -> None:
    sent, header = make(key, body)
    assert not is_valid(key, sent, header, NOW)


def test_a_missing_header_is_refused(key: bytes, body: bytes) -> None:
    assert not is_valid(key, body, None, NOW)


def test_the_edges_of_the_window_pass(key: bytes, body: bytes) -> None:
    assert is_valid(key, body, sign(key, body, at=NOW - WINDOW), NOW)
    assert is_valid(key, body, sign(key, body, at=NOW + WINDOW), NOW)


def test_the_digests_are_compared_in_constant_time(
    key: bytes, body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert is_valid(key, body, sign(key, body), NOW)
    assert len(calls) == 1
    assert all(isinstance(side, bytes) for side in calls[0])
