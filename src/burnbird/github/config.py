"""The GitHub App's secrets, each read once from a file whose path is in the environment."""

import os
from pathlib import Path
from typing import Final

# The environment variable that names the file holding the webhook secret.
WEBHOOK_FILE_VARIABLE: Final = "BURNBIRD_WEBHOOK_SECRET_FILE"


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
