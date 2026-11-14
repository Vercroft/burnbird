import secrets
from pathlib import Path

import pytest

from burnbird.auth.config import (
    BASE_URL_VARIABLE,
    CLIENT_FILE_VARIABLE,
    CLIENT_ID_VARIABLE,
    MIN_STATE_KEY_LENGTH,
    STATE_KEY_FILE_VARIABLE,
    settings_from_environment,
    site_origin,
)
from burnbird.github.config import ConfigError


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A complete, valid environment; each test breaks one part of it."""
    values = {
        CLIENT_FILE_VARIABLE: secrets.token_hex(20),
        STATE_KEY_FILE_VARIABLE: secrets.token_urlsafe(48),
    }
    for variable, value in values.items():
        path = tmp_path / variable
        path.write_text(value + "\n", encoding="utf-8")
        monkeypatch.setenv(variable, str(path))
    monkeypatch.setenv(CLIENT_ID_VARIABLE, "Iv1.example")
    monkeypatch.setenv(BASE_URL_VARIABLE, "https://burnbird.example.com")
    return values


def test_the_settings_are_read_and_the_secrets_kept_out_of_the_repr(
    environment: dict[str, str],
) -> None:
    settings = settings_from_environment()
    assert settings.client_id == "Iv1.example"
    assert settings.client_secret == environment[CLIENT_FILE_VARIABLE]
    assert settings.state_key == environment[STATE_KEY_FILE_VARIABLE]
    assert settings.callback_url == "https://burnbird.example.com/auth/callback"
    for value in environment.values():
        assert value not in repr(settings)


@pytest.mark.parametrize("url", ["https://burnbird.example.com", "https://example.com:8443"])
def test_an_https_origin_is_accepted_as_it_is(url: str) -> None:
    assert site_origin(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://burnbird.example.com",
        "https://burnbird.example.com/",
        "https://burnbird.example.com/app",
        "https://burnbird.example.com?x=1",
        "https://burnbird.example.com#top",
        "https://example@burnbird.example.com",
        "https://Burnbird.example.com",
        "https://burnbird.example.com:443",
        "burnbird.example.com",
    ],
)
def test_anything_but_an_https_origin_is_refused(url: str) -> None:
    with pytest.raises(ConfigError, match=BASE_URL_VARIABLE):
        site_origin(url)


def test_a_missing_client_id_is_refused(
    environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CLIENT_ID_VARIABLE)
    with pytest.raises(ConfigError, match=CLIENT_ID_VARIABLE):
        settings_from_environment()


@pytest.mark.parametrize("variable", [CLIENT_FILE_VARIABLE, STATE_KEY_FILE_VARIABLE])
def test_a_missing_secret_file_is_refused(
    variable: str, environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(variable)
    with pytest.raises(ConfigError, match=variable):
        settings_from_environment()


def test_a_short_state_key_is_refused(
    environment: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "short"
    path.write_text("k" * (MIN_STATE_KEY_LENGTH - 1), encoding="utf-8")
    monkeypatch.setenv(STATE_KEY_FILE_VARIABLE, str(path))
    with pytest.raises(ConfigError, match="at least 32"):
        settings_from_environment()


def test_a_secret_file_that_is_not_text_is_refused_without_its_content(
    environment: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "binary"
    path.write_bytes(b"\xff\xfe" + secrets.token_bytes(40))
    monkeypatch.setenv(CLIENT_FILE_VARIABLE, str(path))
    with pytest.raises(ConfigError, match="not UTF-8") as raised:
        settings_from_environment()
    assert raised.value.__cause__ is None
