"""A message sent to the local Mailpit arrives with the same headers the provider would get.

Needs the Mailpit from CONTRIBUTING.md, named in ``BURNBIRD_TEST_SMTP_URL`` and
``BURNBIRD_TEST_MAILPIT_URL``; without it these tests fail, never skip.
"""

import os
import uuid
from urllib.parse import urlsplit

import httpx2
import pytest

from burnbird.email.config import SmtpSettings
from burnbird.email.message import compose, mime
from burnbird.email.sender import Result, SmtpSender
from tests.email.examples import example_templates

ORIGIN = "https://burnbird.example.invalid"


@pytest.fixture
def mailpit() -> tuple[SmtpSettings, str]:
    smtp, api = (
        os.environ.get("BURNBIRD_TEST_SMTP_URL"),
        os.environ.get("BURNBIRD_TEST_MAILPIT_URL"),
    )
    if not smtp or not api:
        pytest.fail("set BURNBIRD_TEST_SMTP_URL and BURNBIRD_TEST_MAILPIT_URL (CONTRIBUTING.md)")
    parts = urlsplit(smtp)
    assert parts.hostname
    assert parts.port
    return SmtpSettings(host=parts.hostname, port=parts.port), api


def test_a_usage_email_reaches_mailpit_with_its_headers(mailpit: tuple[SmtpSettings, str]) -> None:
    settings, api = mailpit
    reference = uuid.uuid4()
    unsubscribe = f"{ORIGIN}/email/unsubscribe/example-{reference.hex}"
    html, text = example_templates()
    sent = compose(
        reference=reference,
        to="person" + "@" + "example.invalid",
        subject=f"Example {reference.hex}",
        html=html,
        text=text,
        context={"name": "example", "until": "15 December 2026 (UTC)"},
        origin=ORIGIN,
        unsubscribe_url=unsubscribe,
    )

    assert SmtpSender(settings).send(sent) == Result("sent", "smtp:250")

    with httpx2.Client(base_url=api, timeout=10.0) as client:
        found = client.get("/api/v1/search", params={"query": f'subject:"{sent.subject}"'}).json()
        [stored] = found["messages"]
        answer = client.get(f"/api/v1/message/{stored['ID']}/headers").json()
    # Header names are case-insensitive; Mailpit gives some in another case (Message-Id).
    headers = {name.lower(): values for name, values in answer.items()}
    expected = mime(sent)
    for name in (
        "From",
        "To",
        "Subject",
        "Message-ID",
        "List-Unsubscribe",
        "List-Unsubscribe-Post",
    ):
        assert headers[name.lower()] == [expected[name]]
    assert "reply-to" not in headers
