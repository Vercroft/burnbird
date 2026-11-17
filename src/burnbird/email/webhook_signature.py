"""The provider's webhook signature, checked before anything in the body is read.

Each request carries a ``producer-signature`` header,
``ts=<ms>;s=<signature>;s-algorithm=HmacSHA256``. ``s`` is the base64 HMAC-SHA256 of the raw
body under the Authentication Key set on the Agent, URL-encoded (its padding arrives as ``%3D``),
and ``ts`` is when the request was sent, in milliseconds. The algorithm is only checked, never
used to choose one: the header is the sender's to write. (The provider's webhook help page,
"Validating Webhook requests".)

``ts`` is not part of what is signed, so the window below stops only an old request resent as it
was; whoever holds a real request could resend it with a new ``ts``. What a request does is
therefore safe to repeat (``burnbird.email.webhook``).
"""

import base64
import binascii
import hashlib
import hmac
import re
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import unquote

ALGORITHM: Final = "HmacSHA256"
# How far ``ts`` may be from our clock, either way.
WINDOW: Final = timedelta(minutes=5)
_PARTS: Final = frozenset({"ts", "s", "s-algorithm"})
_MILLISECONDS: Final = re.compile(r"[0-9]{1,16}")


def is_valid(key: bytes, body: bytes, header: str | None, now: datetime) -> bool:
    """Return whether ``header`` signs ``body`` with ``key`` and was sent within ``WINDOW``.

    Anything malformed (a missing or repeated part, another algorithm, a time that is not whole
    milliseconds, a signature that is not base64) is refused, never raised. The digests are
    compared as bytes, in constant time.
    """
    parts = _parts(header)
    if parts is None or parts["s-algorithm"] != ALGORITHM:
        return False
    if not _MILLISECONDS.fullmatch(parts["ts"]):
        return False
    sent = int(parts["ts"])
    if abs(now.timestamp() * 1000 - sent) > WINDOW.total_seconds() * 1000:
        return False
    try:
        received = base64.b64decode(unquote(parts["s"]), validate=True)
    except binascii.Error, ValueError:
        return False
    expected = hmac.new(key, body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, received)


def _parts(header: str | None) -> dict[str, str] | None:
    """Return the header's parts by name, or None when a needed one is missing or one repeats."""
    if not header:
        return None
    parts: dict[str, str] = {}
    for part in header.split(";"):
        name, separator, value = part.partition("=")
        name = name.strip()
        if not separator or name in parts:
            return None
        parts[name] = value.strip()
    return parts if parts.keys() >= _PARTS else None
