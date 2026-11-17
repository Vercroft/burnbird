"""The email settings: the provider's API with a bare send token, or a local Mailpit only."""

from pathlib import Path

import pytest

from burnbird.email import config
from burnbird.email.config import SmtpSettings, ZohoSettings, settings_from_environment
from burnbird.github.config import ConfigError

# Invented, built at run time: no token-shaped literal is committed.
TOKEN = "Example" + "SendToken" + "0123456789"


def _key_file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "send-token"
    path.write_text(text)
    return str(path)


def test_the_provider_mode_reads_the_bare_token_and_the_india_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.MODE_VARIABLE, "zoho")
    monkeypatch.setenv(config.API_KEY_FILE_VARIABLE, _key_file(tmp_path, TOKEN + "\n"))
    monkeypatch.delenv(config.API_URL_VARIABLE, raising=False)
    settings = settings_from_environment()
    assert settings == ZohoSettings(url="https://cpaas.zoho.in/v1.1/email", key=TOKEN)
    assert TOKEN not in repr(settings)


@pytest.mark.parametrize(
    "text",
    [
        "Zoho-enczapikey " + TOKEN,
        f'"{TOKEN}"',
        f"'{TOKEN}'",
        TOKEN + " ",
        TOKEN + "\n\n",
        TOKEN[:5] + "\t" + TOKEN[5:],
        TOKEN[:5] + "\x07" + TOKEN[5:],
        TOKEN[:5] + "\u00e9" + TOKEN[5:],
        "Zoho-enczapikey" + TOKEN,
    ],
    ids=[
        "with its prefix",
        "double quotes",
        "single quotes",
        "space",
        "two newlines",
        "tab",
        "control character",
        "not ASCII",
        "prefix without a space",
    ],
)
def test_a_token_with_spaces_quotes_or_a_prefix_is_refused_naming_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setenv(config.MODE_VARIABLE, "zoho")
    monkeypatch.setenv(config.API_KEY_FILE_VARIABLE, _key_file(tmp_path, text))
    with pytest.raises(ConfigError) as refused:
        settings_from_environment()
    assert config.API_KEY_FILE_VARIABLE in str(refused.value)
    assert TOKEN not in str(refused.value)


@pytest.mark.parametrize(
    "url", ["http://cpaas.example.invalid/v1.1/email", "https://", "cpaas.zoho.in/v1.1/email"]
)
def test_an_api_url_that_is_not_https_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv(config.MODE_VARIABLE, "zoho")
    monkeypatch.setenv(config.API_KEY_FILE_VARIABLE, _key_file(tmp_path, TOKEN))
    monkeypatch.setenv(config.API_URL_VARIABLE, url)
    with pytest.raises(ConfigError, match=config.API_URL_VARIABLE):
        settings_from_environment()


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("smtp://127.0.0.1:1025", "127.0.0.1"),
        ("smtp://localhost:1025", "localhost"),
        ("smtp://[::1]:1025", "::1"),
    ],
)
def test_the_mailpit_mode_takes_a_loopback_host(
    monkeypatch: pytest.MonkeyPatch, url: str, host: str
) -> None:
    monkeypatch.setenv(config.MODE_VARIABLE, "smtp")
    monkeypatch.setenv(config.SMTP_URL_VARIABLE, url)
    assert settings_from_environment() == SmtpSettings(host=host, port=1025)


@pytest.mark.parametrize(
    "url",
    [
        "smtp://192.0.2.10:1025",
        "smtp://mail.example.invalid:25",
        "smtp://127.0.0.1",
        "http://localhost:1025",
        "",
    ],
)
def test_the_mailpit_mode_refuses_any_host_but_loopback(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv(config.MODE_VARIABLE, "smtp")
    monkeypatch.setenv(config.SMTP_URL_VARIABLE, url)
    with pytest.raises(ConfigError, match=config.SMTP_URL_VARIABLE):
        settings_from_environment()


@pytest.mark.parametrize("mode", [None, "", "ses", "ZOHO"])
def test_an_unknown_mode_is_refused(monkeypatch: pytest.MonkeyPatch, mode: str | None) -> None:
    if mode is None:
        monkeypatch.delenv(config.MODE_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(config.MODE_VARIABLE, mode)
    with pytest.raises(ConfigError, match=config.MODE_VARIABLE):
        settings_from_environment()


@pytest.mark.parametrize("url", ["smtp://localhost:99999", "smtp://localhost:abc"])
def test_a_port_that_is_not_one_is_refused_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv(config.MODE_VARIABLE, "smtp")
    monkeypatch.setenv(config.SMTP_URL_VARIABLE, url)
    with pytest.raises(ConfigError, match=config.SMTP_URL_VARIABLE):
        settings_from_environment()
