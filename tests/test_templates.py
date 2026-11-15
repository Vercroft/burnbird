"""Every template: it loads, holds no script or inline style, and the environment escapes."""

import re
from importlib import resources
from pathlib import Path

import pytest

from burnbird.templates import ENVIRONMENT

PACKAGE = Path(str(resources.files("burnbird")))
TEMPLATES = sorted(PACKAGE.glob("**/templates/*.html"))


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


def test_an_html_template_escapes_every_value() -> None:
    template = ENVIRONMENT.get_template("account/deleted.html")
    assert template.environment.is_async is False
    assert ENVIRONMENT.from_string("{{ value }}").render(value="<b>") == "&lt;b&gt;"
    # A real page template, rendered with a value holding markup.
    rendered = ENVIRONMENT.get_template("base.html").render(title="a<b")
    assert "a&lt;b · Burnbird" in rendered
    assert "a<b" not in rendered
