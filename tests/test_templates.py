"""Every template: no script, inline style or resource from elsewhere; the environment escapes.

Templates are searched in every ``templates`` folder of the package and every folder below it.
"""

import re
from importlib import resources
from pathlib import Path

import pytest

from burnbird.templates import ENVIRONMENT

PACKAGE = Path(str(resources.files("burnbird")))
TEMPLATES = sorted(PACKAGE.glob("**/templates/**/*.html"))
# A tag that makes the browser load something, and the attribute that names it.
_LOADING = re.compile(
    r"<(?:img|link|script|source|iframe|video|audio|embed|object|track|input)\b[^>]*?"
    r"\s(?:src|srcset|href|data|poster)\s*=\s*[\"']?([^\"'\s>]*)",
    re.IGNORECASE,
)
# Another origin: a scheme ("https:", "data:") or a protocol-relative "//"; or a value only known
# when the page renders, which cannot be shown to be this site.
_ELSEWHERE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|.*\{\{)", re.IGNORECASE)


def loads_from_elsewhere(html: str) -> list[str]:
    """Every resource the HTML would load from anywhere but this site."""
    return [value for value in _LOADING.findall(html) if _ELSEWHERE.match(value)]


def test_the_templates_are_found() -> None:
    names = {path.relative_to(PACKAGE).as_posix() for path in TEMPLATES}
    assert {"templates/base.html", "account/templates/you.html"} <= names


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda path: path.relative_to(PACKAGE).as_posix())
def test_a_template_has_no_script_and_no_inline_style(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    assert "<script" not in text
    assert "<style" not in text
    assert "style=" not in text
    # Event handler attributes are inline script too.
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", text)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda path: path.relative_to(PACKAGE).as_posix())
def test_a_template_loads_nothing_from_another_origin(path: Path) -> None:
    assert loads_from_elsewhere(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "html",
    [
        '<img src="https://elsewhere.example/x.png">',
        "<img alt='x' src='//elsewhere.example/x.png'>",
        '<link rel="stylesheet" href="https://elsewhere.example/a.css">',
        '<script src="http://elsewhere.example/a.js"></script>',
        '<img srcset="https://elsewhere.example/x.png 2x">',
        '<IMG SRC="data:image/png;base64,AAAA">',
        '<img src="{{ picture }}">',
    ],
)
def test_the_check_finds_a_resource_from_another_origin(html: str) -> None:
    assert len(loads_from_elsewhere(html)) == 1


@pytest.mark.parametrize(
    "html",
    [
        '<img src="/static/logo.png">',
        '<link rel="stylesheet" href="/static/site.css">',
        '<a href="https://github.com/Vercroft/burnbird">a link is not a load</a>',
        '<form method="post" action="/you/email"></form>',
    ],
)
def test_the_check_allows_this_site_and_plain_links(html: str) -> None:
    assert loads_from_elsewhere(html) == []


def test_an_html_template_escapes_every_value() -> None:
    template = ENVIRONMENT.get_template("account/deleted.html")
    assert template.environment.is_async is False
    assert ENVIRONMENT.from_string("{{ value }}").render(value="<b>") == "&lt;b&gt;"
    # A real page template, rendered with a value holding markup.
    rendered = ENVIRONMENT.get_template("base.html").render(title="a<b")
    assert "a&lt;b · Burnbird" in rendered
    assert "a<b" not in rendered
