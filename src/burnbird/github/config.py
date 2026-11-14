"""The GitHub App's settings: its client ID, and its secrets, each read from a file."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

# The environment variable that names the file holding the webhook secret.
WEBHOOK_FILE_VARIABLE: Final = "BURNBIRD_WEBHOOK_SECRET_FILE"
CLIENT_ID_VARIABLE: Final = "BURNBIRD_GITHUB_CLIENT_ID"
# The variable that names the file holding the App's private key, in PEM, as GitHub generates it.
PRIVATE_KEY_FILE_VARIABLE: Final = "BURNBIRD_GITHUB_APP_PRIVATE_KEY_FILE"


class ConfigError(Exception):
    """A setting is missing or invalid; the message never holds a secret."""


def read_secret_file(variable: str) -> bytes:
    """Return the secret in the file named in the environment variable ``variable``.

    One trailing newline, as an editor or ``echo`` leaves it, is not part of the secret.
    """
    path = os.environ.get(variable)
    if not path:
        raise ConfigError(f"set {variable} to the secret's file")
    try:
        secret = Path(path).read_bytes().removesuffix(b"\n")
    except OSError as err:
        raise ConfigError(f"cannot read the file named in {variable}") from err
    if not secret:
        raise ConfigError(f"the file named in {variable} is empty")
    return secret


def webhook_secret() -> bytes:
    """Return the webhook secret from the file named in ``BURNBIRD_WEBHOOK_SECRET_FILE``."""
    return read_secret_file(WEBHOOK_FILE_VARIABLE)


def client_id() -> str:
    """Return the App's client ID from ``BURNBIRD_GITHUB_CLIENT_ID``; it is not a secret."""
    value = os.environ.get(CLIENT_ID_VARIABLE)
    if not value:
        raise ConfigError(f"set {CLIENT_ID_VARIABLE} to the GitHub App's client ID")
    return value


@dataclass(frozen=True, slots=True)
class AppSettings:
    """What the App's own calls need; the key is left out of the repr, so no log shows it."""

    # The App JWT's issuer: GitHub recommends the client ID over the App ID.
    client_id: str
    private_key: RSAKey = field(repr=False)


def app_private_key() -> RSAKey:
    """Return the App's RSA private key from ``BURNBIRD_GITHUB_APP_PRIVATE_KEY_FILE``'s file.

    A file that is not an RSA private key is refused at start-up; the error never holds the file's
    contents.
    """
    pem = read_secret_file(PRIVATE_KEY_FILE_VARIABLE)
    try:
        key = RSAKey.import_key(pem)
    except ValueError, TypeError, JoseError:
        raise ConfigError(
            f"the file named in {PRIVATE_KEY_FILE_VARIABLE} is not an RSA key in PEM"
        ) from None
    if not key.is_private:
        raise ConfigError(f"the file named in {PRIVATE_KEY_FILE_VARIABLE} holds a public key only")
    return key


def app_settings() -> AppSettings:
    """Read the App's settings; ``ConfigError`` names the setting, never its value."""
    return AppSettings(client_id=client_id(), private_key=app_private_key())
