"""Logging for Burnbird's commands: every line goes through one formatter that redacts secrets.

The code never logs a token, but a library or a traceback might, so the formatter removes from the
whole formatted line (message, arguments and traceback): anything shaped like a GitHub token, the
value of an ``Authorization`` header, the value after an OAuth secret's name, the ``code`` and
``state`` of a URL's query, and the token of an email's one-click unsubscribe link; an email
address is masked to its first character and domain. No pattern assumes a length: installation
tokens have a longer, stateless form (``ghs_<app id>_<JWT>``) since 2026-04-27. Token prefixes:
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github
"""

import logging
import re
from typing import Final, override

REDACTED: Final = "[redacted]"

_TOKEN: Final = re.compile(r"(?:gh[pousr]|github_pat)_[A-Za-z0-9_.\-]+")
_AUTHORIZATION: Final = re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^'\"\r\n,}]+")
_NAMED: Final = re.compile(
    r"(?i)\b(client_secret|code_verifier|access_token|refresh_token)(['\"]?\s*[:=]\s*['\"]?)"
    r"[^\s'\"&,}]+"
)
# The sign-in callback's URL holds the single-use code and the state, and an access log or an HTTP
# client's request line would print it.
_QUERY: Final = re.compile(r"([?&](?:code|state)=)[^&\s\"']+")
# An unsubscribe link's token turns a user's warnings off; an access log or a traceback would print
# the path. The path is kept, so the line still says what was asked.
_UNSUBSCRIBE: Final = re.compile(r"(/email/unsubscribe/)[^/?#\s\"'<>]+")
# An email address keeps only its first character and its domain. The code never logs one; this
# catches one a library or a traceback might print.
# Bounded parts (RFC 5321's 64-character local part, 63-character labels) keep it linear on a
# long line. The site's own domain is left as it is: a message's ID names its outbox row.
_EMAIL: Final = re.compile(
    r"(?<![\w.%+'-])(\w)[\w.%+'-]{0,63}@((?:[\w-]{1,63}\.){1,8}[\w-]{2,63})"
)
_OWN_DOMAIN: Final = "burnbird.racingcrow.com"
# Authlib logs the PKCE verifier at DEBUG, so its logger never goes below INFO.
_QUIET: Final = {"authlib": logging.INFO}


def redact(text: str) -> str:
    """Return ``text`` with every secret-shaped value replaced by ``[redacted]``."""
    text = _AUTHORIZATION.sub(rf"\1{REDACTED}", text)
    text = _NAMED.sub(rf"\1\2{REDACTED}", text)
    text = _QUERY.sub(rf"\1{REDACTED}", text)
    text = _UNSUBSCRIBE.sub(rf"\1{REDACTED}", text)
    text = _EMAIL.sub(_mask, text)
    return _TOKEN.sub(REDACTED, text)


def _mask(found: re.Match[str]) -> str:
    if found[2].lower() == _OWN_DOMAIN:
        return found[0]
    return f"{found[1]}***@{found[2]}"


class RedactingFormatter(logging.Formatter):
    """A formatter whose whole output, traceback included, is redacted."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure(level: int = logging.INFO) -> None:
    """Send every log line to standard error through the redacting formatter."""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name, floor in _QUIET.items():
        logging.getLogger(name).setLevel(floor)
