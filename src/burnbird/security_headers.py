"""The security headers every response carries.

The policy allows this site's own resources only, no inline script or style, no framing, forms
that post only to this site and no ``<base>`` element. ``Referrer-Policy: same-origin`` keeps the
``Origin`` header on our own POSTs, which the CSRF check needs (``no-referrer`` would make the
browser send ``Origin: null``). A pure ASGI middleware, so it adds the headers to redirects and
error answers too, and never buffers a body.
"""

from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY: Final = (
    "default-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; "
    "base-uri 'none'"
)
HEADERS: Final = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
)


class SecurityHeadersMiddleware:
    """Add ``HEADERS`` to the start of every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the app."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, adding the headers as the response starts."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in HEADERS}
                kept = [(n, v) for n, v in message.get("headers", []) if n.lower() not in names]
                message["headers"] = [*kept, *HEADERS]
            await send(message)

        await self.app(scope, receive, send_with_headers)
