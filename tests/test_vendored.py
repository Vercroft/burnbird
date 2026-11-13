"""The vendored image-age check and its test are unchanged copies, byte for byte."""

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The sha256 values recorded with the files where they are maintained.
RECORDED = {
    "scripts/check-image-age.py": (
        "6147c644de29592d14ad5999e4be235b838a5d97e88a83602666a9d05627aa71"
    ),
    "tests/test_check_image_age.py": (
        "79972aef6fc61d1dea37aa6f8b6396fbf77205fe7d5a5feeeeffce5435ef4460"
    ),
}


@pytest.mark.parametrize(("path", "sha256"), RECORDED.items())
def test_vendored_file_matches_its_recorded_sha256(path: str, sha256: str) -> None:
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == sha256
