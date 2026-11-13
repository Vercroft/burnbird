import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from burnbird.database import DATABASE_URL_VARIABLE
from burnbird.github.config import WEBHOOK_FILE_VARIABLE
from burnbird.web import MAX_BODY_BYTES, app_from_environment


def test_the_app_is_built_from_the_environment(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "webhook-secret"
    secret_file.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    monkeypatch.setenv(WEBHOOK_FILE_VARIABLE, str(secret_file))
    monkeypatch.setenv(DATABASE_URL_VARIABLE, database_url)
    app = app_from_environment()
    with TestClient(app) as client:
        # The body limit is on: an oversized body is refused before its signature is checked.
        too_large = b"x" * (MAX_BODY_BYTES + 1)
        assert client.post("/github/webhook", content=too_large).status_code == 413
        # The webhook route is there, and refuses an unsigned delivery.
        assert client.post("/github/webhook", content=b"{}").status_code == 403
        # No generated API pages.
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
