import hashlib
import hmac
import secrets

from burnbird.github.signature import is_valid


def test_githubs_published_example_validates() -> None:
    # GitHub's own test values, published to check an implementation; not a secret.
    # https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
    secret = b"It's a Secret to Everybody"
    header = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert is_valid(secret, b"Hello, World!", header)


def test_a_signature_made_with_hmac_validates() -> None:
    secret, body = secrets.token_bytes(32), b'{"action":"completed"}'
    header = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert is_valid(secret, body, header)


def test_a_wrong_or_missing_signature_is_refused() -> None:
    secret, body = secrets.token_bytes(32), b"{}"
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert not is_valid(secret, body, None)
    assert not is_valid(secret, body, digest)
    assert not is_valid(secret, body, "sha1=" + digest)
    assert not is_valid(secret, body + b" ", "sha256=" + digest)
    assert not is_valid(secrets.token_bytes(32), body, "sha256=" + digest)


def test_a_header_that_is_not_ascii_is_refused_not_raised() -> None:
    assert not is_valid(secrets.token_bytes(32), b"{}", "sha256=é")
