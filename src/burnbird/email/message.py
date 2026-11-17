"""One email: its content from a template, and the two forms it leaves in.

The provider gets JSON, in the shape of its send API page; a local Mailpit gets a MIME message
over SMTP. Both carry the same sender, subject, bodies and headers. A usage email carries RFC 8058
one-click unsubscribe: one HTTPS URI in ``List-Unsubscribe``, and
``List-Unsubscribe-Post: List-Unsubscribe=One-Click``.
A service email carries neither. There is no Reply-To: replies to the sender's address are
forwarded to the company inbox. Open and click tracking are always off, and the layout links only
to the site and fetches nothing from anywhere, so reading an email tells no one anything.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr
from typing import Any, Final

from jinja2 import Template

SENDER_NAME: Final = "Burnbird"
SENDER_DOMAIN: Final = "burnbird.racingcrow.com"
SENDER_ADDRESS: Final = f"hello@{SENDER_DOMAIN}"
FOOTER: Final = "Burnbird by Vercroft · burnbird.racingcrow.com"
ONE_CLICK: Final = "List-Unsubscribe=One-Click"
# SMTP line endings, and RFC 5322's hard limit of 998 characters a line instead of the default 78:
# a header with no space in it (the unsubscribe URL) is otherwise rewritten as an encoded word,
# which a mail client cannot read as a URI.
MIME_POLICY: Final = SMTP.clone(max_line_length=998)


@dataclass(frozen=True, slots=True)
class Message:
    """A rendered email; its address and bodies never reach a repr, so never a log."""

    # The outbox row's ID: the provider's ``client_reference`` and the Message-ID's local part.
    reference: uuid.UUID
    to: str = field(repr=False)
    subject: str
    text: str = field(repr=False)
    html: str = field(repr=False)
    # Set for a usage email only.
    unsubscribe_url: str | None = None

    def headers(self) -> dict[str, str]:
        """Return the headers added to the standard ones: one-click unsubscribe, if any."""
        if self.unsubscribe_url is None:
            return {}
        return {
            "List-Unsubscribe": f"<{self.unsubscribe_url}>",
            "List-Unsubscribe-Post": ONE_CLICK,
        }


def unsubscribe_url(origin: str, token: str) -> str:
    """Return the one-click unsubscribe URL for a token: on the site, the token in the path."""
    return f"{origin}/email/unsubscribe/{token}"


def compose(
    *,
    reference: uuid.UUID,
    to: str,
    subject: str,
    html: Template,
    text: Template,
    context: Mapping[str, Any],
    origin: str,
    unsubscribe_url: str | None,
) -> Message:
    """Render both bodies; each template extends ``email/layout.html`` or ``email/layout.txt``."""
    values = {
        **context,
        "subject": subject,
        "origin": origin,
        "unsubscribe_url": unsubscribe_url,
        "footer": FOOTER,
    }
    return Message(
        reference=reference,
        to=to,
        subject=subject,
        text=text.render(values),
        html=html.render(values),
        unsubscribe_url=unsubscribe_url,
    )


def zoho_payload(message: Message) -> dict[str, Any]:
    """Return the provider's send request body for the message."""
    payload: dict[str, Any] = {
        "from": {"address": SENDER_ADDRESS, "name": SENDER_NAME},
        "to": [{"email_address": {"address": message.to}}],
        "subject": message.subject,
        "textbody": message.text,
        "htmlbody": message.html,
        "track_opens": False,
        "track_clicks": False,
        "client_reference": str(message.reference),
    }
    if headers := message.headers():
        payload["mime_headers"] = headers
    return payload


def mime(message: Message) -> EmailMessage:
    """Return the message as MIME, for SMTP: plain text first, then HTML."""
    mail = EmailMessage(policy=MIME_POLICY)
    mail["From"] = formataddr((SENDER_NAME, SENDER_ADDRESS))
    mail["To"] = message.to
    mail["Subject"] = message.subject
    mail["Message-ID"] = f"<{message.reference}@{SENDER_DOMAIN}>"
    for name, value in message.headers().items():
        mail[name] = value
    mail.set_content(message.text)
    mail.add_alternative(message.html, subtype="html")
    return mail
