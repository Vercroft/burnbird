import base64
import json
import logging
import secrets
import sys

import pytest

from burnbird.logs import REDACTED, RedactingFormatter, configure, redact


def _jwt_like() -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'iss': 1})}.{secrets.token_urlsafe(256)}"


# Every value is built from parts at run time: no token-shaped literal is committed.
TOKENS = {
    "user": "gh" + "u_" + secrets.token_urlsafe(30),
    "refresh": "gh" + "r_" + secrets.token_urlsafe(60),
    "classic installation": "gh" + "s_" + secrets.token_urlsafe(30),
    # GitHub's stateless installation token, ghs_<app id>_<JWT>: dots, and no fixed length.
    "stateless installation": "gh" + "s_" + "123456_" + _jwt_like(),
    "oauth": "gh" + "o_" + secrets.token_urlsafe(30),
    "classic personal": "gh" + "p_" + secrets.token_urlsafe(30),
    "fine-grained personal": "github" + "_pat_" + secrets.token_urlsafe(60),
}


@pytest.mark.parametrize("token", list(TOKENS.values()), ids=list(TOKENS))
def test_every_kind_of_github_token_is_redacted_whole(token: str) -> None:
    assert redact(f"before {token} after") == f"before {REDACTED} after"


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: Bearer {secret}",
        "headers={{'authorization': 'Bearer {secret}'}}",
        'headers={{"Authorization": "token {secret}"}}',
        "authorization={secret}",
        "client_secret={secret}&code=x",
        "{{'code_verifier': '{secret}'}}",
        "Using code_verifier: '{secret}'",
        '{{"access_token": "{secret}", "token_type": "bearer"}}',
        "refresh_token={secret}",
        "GET /auth/callback?code={secret}&state=x HTTP/1.1",
        "GET /auth/callback?state={secret}&code=x HTTP/1.1",
    ],
)
def test_secrets_in_headers_bodies_and_urls_are_redacted(line: str) -> None:
    secret = secrets.token_hex(20)
    redacted = redact(line.format(secret=secret))
    assert secret not in redacted
    assert REDACTED in redacted


def test_ordinary_text_is_left_alone() -> None:
    line = "sign-in: user 7001, 2 installations (0 skipped), 3 repositories; status code 502"
    assert redact(line) == line


def test_the_formatter_redacts_arguments_and_tracebacks() -> None:
    token = TOKENS["stateless installation"]
    formatter = RedactingFormatter("%(message)s")
    try:
        raise ValueError(f"refused {token}")
    except ValueError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "sent %s", (token,), sys.exc_info()
        )
    output = formatter.format(record)
    assert token not in output
    assert output.count(REDACTED) == 2


@pytest.mark.usefixtures("restore_logging")
def test_authlib_never_logs_below_info_even_at_debug() -> None:
    # Authlib logs the PKCE verifier at DEBUG.
    configure(logging.DEBUG)
    assert logging.getLogger().isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("authlib").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("authlib.integrations.base_client").isEnabledFor(logging.DEBUG)


@pytest.mark.usefixtures("restore_logging")
def test_configure_sends_every_line_through_the_redacting_formatter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure()
    token = TOKENS["user"]
    logging.getLogger("anything").warning("token %s", token)
    written = capsys.readouterr().err
    assert token not in written
    assert f"WARNING anything: token {REDACTED}" in written
