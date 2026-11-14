import secrets
from collections.abc import Callable
from pathlib import Path

import pytest
from joserfc.jwk import ECKey, RSAKey

from burnbird.github.config import (
    CLIENT_ID_VARIABLE,
    PRIVATE_KEY_FILE_VARIABLE,
    WEBHOOK_FILE_VARIABLE,
    ConfigError,
    app_private_key,
    app_settings,
    webhook_secret,
)


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


def _key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    path = tmp_path / "app-private-key.pem"
    path.write_bytes(content)
    monkeypatch.setenv(PRIVATE_KEY_FILE_VARIABLE, str(path))


def test_the_apps_private_key_is_read_from_its_file(
    rsa_key: RSAKey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _key_file(tmp_path, monkeypatch, rsa_key.as_pem(private=True) + b"\n")
    key = app_private_key()
    assert key.is_private
    assert key.as_dict(private=False) == rsa_key.as_dict(private=False)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda key: key.as_pem(private=False), id="public key only"),
        pytest.param(lambda key: ECKey.generate_key().as_pem(private=True), id="not RSA"),
        pytest.param(lambda key: b"not a key " + secrets.token_hex(16).encode(), id="not a key"),
        pytest.param(lambda key: b"", id="empty"),
    ],
)
def test_a_file_that_is_not_an_rsa_private_key_is_refused_without_its_contents(
    make: Callable[[RSAKey], bytes],
    rsa_key: RSAKey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = make(rsa_key)
    _key_file(tmp_path, monkeypatch, content)
    with pytest.raises(ConfigError, match=PRIVATE_KEY_FILE_VARIABLE) as raised:
        app_private_key()
    shown = f"{raised.value} {raised.value!r}"
    for line in content.decode().splitlines():
        if len(line) > 8:
            assert line not in shown
    assert raised.value.__suppress_context__ or raised.value.__context__ is None


def test_a_missing_key_setting_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLIENT_ID_VARIABLE, "Iv23.example")
    monkeypatch.delenv(PRIVATE_KEY_FILE_VARIABLE, raising=False)
    with pytest.raises(ConfigError, match=PRIVATE_KEY_FILE_VARIABLE):
        app_settings()


def test_a_missing_client_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLIENT_ID_VARIABLE, raising=False)
    with pytest.raises(ConfigError, match=CLIENT_ID_VARIABLE):
        app_settings()


def test_the_settings_never_show_the_key(
    rsa_key: RSAKey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CLIENT_ID_VARIABLE, "Iv23.example")
    _key_file(tmp_path, monkeypatch, rsa_key.as_pem(private=True))
    settings = app_settings()
    assert settings.client_id == "Iv23.example"
    assert repr(settings) == "AppSettings(client_id='Iv23.example')"


def test_a_key_file_that_does_not_exist_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PRIVATE_KEY_FILE_VARIABLE, str(tmp_path / "absent.pem"))
    with pytest.raises(ConfigError, match="cannot read"):
        app_private_key()
