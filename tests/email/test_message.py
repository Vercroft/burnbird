"""An email's content and both forms it leaves in: the provider's JSON, and a MIME message."""

import re
import uuid
from html.parser import HTMLParser
from typing import override

import pytest

from burnbird.email import message
from burnbird.email.message import FOOTER, Message, compose, mime, zoho_payload
from tests.email.examples import example_templates

ORIGIN = "https://burnbird.example.invalid"
REFERENCE = uuid.UUID(int=7)
TO = "person" + "@" + "example.invalid"
UNSUBSCRIBE = f"{ORIGIN}/email/unsubscribe/example-token"


def _message(unsubscribe_url: str | None = UNSUBSCRIBE) -> Message:
    html, text = example_templates()
    return compose(
        reference=REFERENCE,
        to=TO,
        subject="Example subject",
        html=html,
        text=text,
        context={"name": "<b>example</b>"},
        origin=ORIGIN,
        unsubscribe_url=unsubscribe_url,
    )


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.tags: set[str] = set()

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        self.hrefs.extend(value or "" for name, value in attrs if name in ("href", "src"))


def test_a_usage_email_carries_both_one_click_headers() -> None:
    assert _message().headers() == {
        "List-Unsubscribe": f"<{UNSUBSCRIBE}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def test_a_service_email_carries_no_unsubscribe_header() -> None:
    assert _message(unsubscribe_url=None).headers() == {}


def test_the_provider_payload_tracks_nothing_and_names_the_outbox_row() -> None:
    payload = zoho_payload(_message())
    assert payload["track_opens"] is False
    assert payload["track_clicks"] is False
    assert payload["client_reference"] == str(REFERENCE)
    assert payload["from"] == {"address": "hello@burnbird.racingcrow.com", "name": "Burnbird"}
    assert payload["to"] == [{"email_address": {"address": TO}}]
    assert payload["subject"] == "Example subject"
    assert payload["mime_headers"] == _message().headers()
    assert "reply_to" not in payload


def test_a_service_email_s_payload_has_no_custom_headers() -> None:
    assert "mime_headers" not in zoho_payload(_message(unsubscribe_url=None))


def test_the_mime_message_has_the_same_headers_and_no_reply_to() -> None:
    sent = mime(_message())
    assert sent["From"] == "Burnbird <hello@burnbird.racingcrow.com>"
    assert sent["To"] == TO
    assert sent["Subject"] == "Example subject"
    assert sent["List-Unsubscribe"] == f"<{UNSUBSCRIBE}>"
    assert sent["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert sent["Message-ID"] == f"<{REFERENCE}@burnbird.racingcrow.com>"
    assert sent["Reply-To"] is None
    assert [part.get_content_type() for part in sent.iter_parts()] == ["text/plain", "text/html"]


@pytest.mark.parametrize("unsubscribe_url", [UNSUBSCRIBE, None], ids=["usage", "service"])
def test_every_link_is_on_the_origin_and_nothing_is_fetched_from_elsewhere(
    unsubscribe_url: str | None,
) -> None:
    sent = _message(unsubscribe_url)
    links = _Links()
    links.feed(sent.html)
    assert links.hrefs, "the layout links somewhere"
    assert all(href.startswith(f"{ORIGIN}/") for href in links.hrefs)
    assert not links.tags & {"img", "script", "link", "iframe", "style"}
    for body in (sent.html, sent.text):
        for url in re.findall(r"https?://[^\s\"'<>]+", body):
            assert url.startswith(f"{ORIGIN}/")


def test_the_footer_is_exact_and_values_are_escaped_in_html_only() -> None:
    sent = _message()
    assert FOOTER == "Burnbird by Vercroft · burnbird.racingcrow.com"
    assert FOOTER in sent.text
    assert FOOTER in sent.html
    assert "Hello &lt;b&gt;example&lt;/b&gt;" in sent.html
    assert "Hello <b>example</b>" in sent.text


def test_only_a_usage_email_shows_the_unsubscribe_link() -> None:
    assert UNSUBSCRIBE in _message().text
    assert UNSUBSCRIBE in _message().html
    assert "unsubscribe" not in _message(unsubscribe_url=None).text.lower()


def test_an_address_and_the_bodies_never_reach_a_repr() -> None:
    shown = repr(_message())
    assert TO not in shown
    assert "Hello" not in shown


def test_the_unsubscribe_url_is_on_the_origin() -> None:
    assert message.unsubscribe_url(ORIGIN, "example-token") == UNSUBSCRIBE


def test_a_long_unsubscribe_url_stays_one_plain_header_line() -> None:
    # A URL longer than 78 characters with no space in it, as a real token makes it.
    long_url = f"{ORIGIN}/email/unsubscribe/{'x' * 120}"
    sent = mime(_message(unsubscribe_url=long_url)).as_string()
    assert f"List-Unsubscribe: <{long_url}>\r\n" in sent
    assert "=?utf-8?" not in sent
