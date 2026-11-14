import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from burnbird.auth.config import (
    BASE_URL_VARIABLE,
    CLIENT_FILE_VARIABLE,
    CLIENT_ID_VARIABLE,
    STATE_KEY_FILE_VARIABLE,
)
from burnbird.database import DATABASE_URL_VARIABLE
from burnbird.github.config import WEBHOOK_FILE_VARIABLE
from burnbird.web import MAX_BODY_BYTES, app_from_environment


@pytest.mark.usefixtures("restore_logging")
def test_the_app_is_built_from_the_environment(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable, value in (
        (WEBHOOK_FILE_VARIABLE, secrets.token_hex(32)),
        (CLIENT_FILE_VARIABLE, secrets.token_hex(20)),
        (STATE_KEY_FILE_VARIABLE, secrets.token_urlsafe(48)),
    ):
        secret_file = tmp_path / variable
        secret_file.write_text(value + "\n", encoding="utf-8")
        monkeypatch.setenv(variable, str(secret_file))
    monkeypatch.setenv(CLIENT_ID_VARIABLE, "Iv1.example")
    monkeypatch.setenv(BASE_URL_VARIABLE, "https://burnbird.test")
    monkeypatch.setenv(DATABASE_URL_VARIABLE, database_url)
    app = app_from_environment()
    with TestClient(app, base_url="https://burnbird.test", follow_redirects=False) as client:
        # The body limit is on: an oversized body is refused before its signature is checked.
        too_large = b"x" * (MAX_BODY_BYTES + 1)
        assert client.post("/github/webhook", content=too_large).status_code == 413
        # The webhook route is there, and refuses an unsigned delivery.
        assert client.post("/github/webhook", content=b"{}").status_code == 403
        # No generated API pages.
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
        # Sign-in is there, and sends the visitor to GitHub with the client ID from the setting.
        login = client.get("/auth/login")
        assert login.status_code == 302
        assert login.headers["location"].startswith(
            "https://github.com/login/oauth/authorize?response_type=code&client_id=Iv1.example&"
        )
