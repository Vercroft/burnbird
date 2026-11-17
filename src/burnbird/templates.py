"""The one Jinja2 environment for every page and email: autoescaped HTML, templates in the package.

Shared templates (the base layout) are in ``burnbird/templates``; a feature's own templates are in
its folder's ``templates`` and are named with the feature as a prefix, such as
``account/you.html``; an email's layout is ``email/layout.html`` and ``email/layout.txt``.
Autoescaping is on for every ``.html`` template, so a value is always shown
as text: "When autoescaping is enabled, everything is escaped by default except for values
explicitly marked as safe" (Jinja's template documentation, "HTML Escaping"). An undefined name
fails the render instead of showing nothing.
"""

from typing import Any, Final

from fastapi.responses import HTMLResponse
from jinja2 import (
    ChoiceLoader,
    Environment,
    PackageLoader,
    PrefixLoader,
    StrictUndefined,
    select_autoescape,
)

ENVIRONMENT: Final = Environment(
    loader=ChoiceLoader(
        [
            PrefixLoader(
                {
                    "account": PackageLoader("burnbird.account", "templates"),
                    "email": PackageLoader("burnbird.email", "templates"),
                }
            ),
            PackageLoader("burnbird", "templates"),
        ]
    ),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
    undefined=StrictUndefined,
)


def page(template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    """Render a page; it is never cached, since every page may show the user's own data."""
    return HTMLResponse(
        ENVIRONMENT.get_template(template).render(**context),
        status_code,
        headers={"Cache-Control": "no-store"},
    )
