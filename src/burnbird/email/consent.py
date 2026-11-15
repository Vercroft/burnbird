"""The consent wording shown next to the email switch, and its version.

The version is the first 12 hex digits of the SHA-256 of ``consent.txt`` itself, so a changed
wording is a new version without anyone remembering to raise it. Every consent row names the
version the user saw.
"""

import hashlib
from importlib import resources
from typing import Final

_RAW: Final = resources.files(__package__).joinpath("consent.txt").read_bytes()

TEXT: Final = _RAW.decode("utf-8").strip()
TEXT_VERSION: Final = hashlib.sha256(_RAW).hexdigest()[:12]
