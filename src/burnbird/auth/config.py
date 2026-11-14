"""Sign-in settings: the App's client ID and secret, the OAuth state key and the site's origin.

The client ID and the site's address come from the environment; the client secret and the state
key are read from files named in the environment, like the webhook secret.
"""

import os
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from burnbird.github.config import CLIENT_ID_VARIABLE as CLIENT_ID_VARIABLE
from burnbird.github.config import ConfigError, client_id, read_secret_file

# The variable that names the file holding the App's client secret.
CLIENT_FILE_VARIABLE: Final = "BURNBIRD_GITHUB_CLIENT_SECRET_FILE"
STATE_KEY_FILE_VARIABLE: Final = "BURNBIRD_OAUTH_STATE_KEY_FILE"
BASE_URL_VARIABLE: Final = "BURNBIRD_BASE_URL"

# The OAuth state cookie is signed with this key; 32 bytes or more, for example from
# ``openssl rand -base64 48``.
MIN_STATE_KEY_LENGTH: Final = 32


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """What sign-in needs; the two secrets are left out of the repr, so a log never shows them."""

    client_id: str
    client_secret: str = field(repr=False)
    state_key: str = field(repr=False)
    # The site's origin, ``https://<host>`` or ``https://<host>:<port>``: the base of the callback
    # URL and the only Origin a POST may come from.
    origin: str

    @property
    def callback_url(self) -> str:
        """The callback URL registered with the GitHub App."""
        return f"{self.origin}/auth/callback"


def site_origin(url: str) -> str:
    """Return the origin of the site's base URL, refusing anything but ``https://<host>[:port]``.

    The value must already be in the form a browser sends in ``Origin`` (lower case, no path, no
    trailing slash), so the POST check can compare strings exactly.
    """
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or url != url.lower()
        or url != f"{parts.scheme}://{parts.netloc}"
        # A browser leaves the default port out of Origin, so ":443" would refuse every POST.
        or parts.port == 443
    ):
        raise ConfigError(f"{BASE_URL_VARIABLE} must be https://<host> or https://<host>:<port>")
    return url


def settings_from_environment() -> AuthSettings:
    """Read the sign-in settings; ``ConfigError`` names the setting, never its value."""
    app_client_id = client_id()
    origin = site_origin(os.environ.get(BASE_URL_VARIABLE, ""))
    return AuthSettings(
        client_id=app_client_id,
        client_secret=_text(CLIENT_FILE_VARIABLE),
        state_key=state_key(_text(STATE_KEY_FILE_VARIABLE)),
        origin=origin,
    )


def state_key(key: str) -> str:
    """Return the state key once it is long enough."""
    if len(key) < MIN_STATE_KEY_LENGTH:
        raise ConfigError(
            f"the file named in {STATE_KEY_FILE_VARIABLE} must hold at least "
            f"{MIN_STATE_KEY_LENGTH} characters"
        )
    return key


def _text(variable: str) -> str:
    try:
        return read_secret_file(variable).decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"the file named in {variable} is not UTF-8 text") from None
