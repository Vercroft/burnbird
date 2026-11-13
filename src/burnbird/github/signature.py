"""GitHub's webhook signature: an HMAC-SHA256 hex digest of the raw body, prefixed ``sha256=``.

https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
"""

import hashlib
import hmac


def is_valid(secret: bytes, body: bytes, header: str | None) -> bool:
    """Return whether ``header`` signs ``body`` with ``secret``, compared in constant time.

    Bytes are compared, not text: ``hmac.compare_digest`` takes text only when it is ASCII, and a
    header holds whatever the sender put in it.
    https://docs.python.org/3/library/hmac.html#hmac.compare_digest
    """
    if header is None:
        return False
    expected = b"sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest().encode()
    return hmac.compare_digest(expected, header.encode())
