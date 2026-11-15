import hashlib
from pathlib import Path

from burnbird.email import consent, preferences
from burnbird.models import EMAIL_KINDS

CONSENT_FILE = Path(consent.__file__).with_name("consent.txt")


def test_the_version_is_the_start_of_the_wording_s_sha256() -> None:
    raw = CONSENT_FILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest()[:12] == consent.TEXT_VERSION
    assert raw.decode("utf-8").strip() == consent.TEXT


def test_the_wording_names_only_what_exists() -> None:
    assert "Run-out warnings" in consent.TEXT
    assert "weekly" not in consent.TEXT.lower()
    assert "news" not in consent.TEXT.lower()


def test_there_is_a_default_for_every_kind() -> None:
    assert tuple(preferences.DEFAULTS) == EMAIL_KINDS
