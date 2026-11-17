"""Email settings: send through the provider's API, or to a local Mailpit (development, tests).

``BURNBIRD_EMAIL_MODE`` is ``zoho`` or ``smtp``. In ``zoho`` mode the send token is read from the
file named in ``BURNBIRD_ZOHO_API_KEY_FILE``, bare: the ``Zoho-enczapikey`` prefix, spaces, line
breaks, quotes, control and non-ASCII characters are refused, so a token pasted with them fails at
start, not at the first send.
The API URL defaults to the provider's India data centre, from its "Multiple Data Centers" page.
``smtp`` mode sends only to a loopback host, so it can never reach a real mail server.
"""

import os
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from burnbird.github.config import ConfigError, read_secret_file

MODE_VARIABLE: Final = "BURNBIRD_EMAIL_MODE"
API_URL_VARIABLE: Final = "BURNBIRD_ZOHO_API_URL"
API_KEY_FILE_VARIABLE: Final = "BURNBIRD_ZOHO_API_KEY_FILE"
SMTP_URL_VARIABLE: Final = "BURNBIRD_SMTP_URL"
DEFAULT_API_URL: Final = "https://cpaas.zoho.in/v1.1/email"
# The scheme the provider puts before the token in its Authorization header; never in the file.
AUTHORIZATION_SCHEME: Final = "Zoho-enczapikey"
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True, slots=True)
class ZohoSettings:
    """The provider's send URL and the Agent's send token; the token never reaches a repr."""

    url: str
    key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """A local Mailpit's SMTP host and port."""

    host: str
    port: int


def settings_from_environment() -> ZohoSettings | SmtpSettings:
    """Read the email settings; ``ConfigError`` names the setting, never its value."""
    mode = os.environ.get(MODE_VARIABLE)
    if mode == "zoho":
        return ZohoSettings(
            url=api_url(os.environ.get(API_URL_VARIABLE, DEFAULT_API_URL)), key=_send_token()
        )
    if mode == "smtp":
        return smtp_settings(os.environ.get(SMTP_URL_VARIABLE, ""))
    raise ConfigError(f"set {MODE_VARIABLE} to zoho or smtp")


def api_url(url: str) -> str:
    """Return the provider's send URL once it is an https URL."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigError(f"{API_URL_VARIABLE} must be an https:// URL")
    return url


def smtp_settings(url: str) -> SmtpSettings:
    """Return Mailpit's host and port from ``smtp://<loopback host>:<port>``."""
    refused = ConfigError(
        f"{SMTP_URL_VARIABLE} must be smtp://<loopback host>:<port>: in smtp mode email goes"
        " only to a local Mailpit"
    )
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise refused from None
    if parts.scheme != "smtp" or parts.hostname not in LOOPBACK_HOSTS or port is None:
        raise refused
    return SmtpSettings(host=parts.hostname, port=port)


def _send_token() -> str:
    try:
        token = read_secret_file(API_KEY_FILE_VARIABLE).decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"the file named in {API_KEY_FILE_VARIABLE} is not UTF-8 text") from None
    if (
        token.startswith(AUTHORIZATION_SCHEME)
        or not (token.isascii() and token.isprintable())
        or any(character.isspace() or character in "\"'`" for character in token)
    ):
        raise ConfigError(
            f"the file named in {API_KEY_FILE_VARIABLE} must hold the bare send token only: no"
            " prefix, spaces, line breaks or quotes"
        )
    return token
