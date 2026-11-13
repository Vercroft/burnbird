"""The GitHub App's secrets, each read once from a file whose path is in the environment."""

import os
from pathlib import Path
from typing import Final

# The environment variable that names the file holding the webhook secret.
WEBHOOK_FILE_VARIABLE: Final = "BURNBIRD_WEBHOOK_SECRET_FILE"


class ConfigError(Exception):
    """A secret file is not named, cannot be read or is empty; the message never holds a secret."""


def webhook_secret() -> bytes:
    """Return the webhook secret from the file named in ``BURNBIRD_WEBHOOK_SECRET_FILE``.

    One trailing newline, as an editor or ``echo`` leaves it, is not part of the secret.
    """
    path = os.environ.get(WEBHOOK_FILE_VARIABLE)
    if not path:
        raise ConfigError(f"set {WEBHOOK_FILE_VARIABLE} to the webhook secret's file")
    try:
        secret = Path(path).read_bytes().removesuffix(b"\n")
    except OSError as err:
        raise ConfigError(f"cannot read the file named in {WEBHOOK_FILE_VARIABLE}") from err
    if not secret:
        raise ConfigError(f"the file named in {WEBHOOK_FILE_VARIABLE} is empty")
    return secret
