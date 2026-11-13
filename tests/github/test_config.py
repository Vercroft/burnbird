import secrets
from pathlib import Path

import pytest

from burnbird.github.config import WEBHOOK_FILE_VARIABLE, ConfigError, webhook_secret


def test_the_secret_is_read_and_one_trailing_newline_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = secrets.token_hex(32).encode()
    path = tmp_path / "webhook-secret"
    path.write_bytes(secret + b"\n\n")
    monkeypatch.setenv(WEBHOOK_FILE_VARIABLE, str(path))
    assert webhook_secret() == secret + b"\n"


def test_a_missing_variable_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WEBHOOK_FILE_VARIABLE, raising=False)
    with pytest.raises(ConfigError, match=WEBHOOK_FILE_VARIABLE):
        webhook_secret()


def test_a_missing_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WEBHOOK_FILE_VARIABLE, str(tmp_path / "absent"))
    with pytest.raises(ConfigError, match="cannot read"):
        webhook_secret()


@pytest.mark.parametrize("content", [b"", b"\n"])
def test_an_empty_secret_is_refused(
    content: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "webhook-secret"
    path.write_bytes(content)
    monkeypatch.setenv(WEBHOOK_FILE_VARIABLE, str(path))
    with pytest.raises(ConfigError, match="empty"):
        webhook_secret()


def test_an_error_never_holds_the_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = secrets.token_hex(32)
    # A directory cannot be read as a file; its name is the secret, so a leak would show.
    monkeypatch.setenv(WEBHOOK_FILE_VARIABLE, str(tmp_path / secret / "missing"))
    with pytest.raises(ConfigError) as raised:
        webhook_secret()
    assert secret not in str(raised.value)
