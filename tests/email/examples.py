"""Example email templates for the tests, loaded by name as the real ones are."""

from jinja2 import ChoiceLoader, DictLoader, Template

from burnbird.templates import ENVIRONMENT


def example_templates() -> tuple[Template, Template]:
    """An email body as a later email will give it, loaded by name as the real ones are.

    The layout supplies everything around it; ``.html`` is autoescaped and ``.txt`` is not.
    """
    assert ENVIRONMENT.loader is not None
    environment = ENVIRONMENT.overlay(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        "email/example.html": "{% extends 'email/layout.html' %}"
                        "{% block body %}<p>Hello {{ name }}</p>{% endblock %}",
                        "email/example.txt": "{% extends 'email/layout.txt' %}"
                        "{% block body %}Hello {{ name }}{% endblock %}",
                    }
                ),
                ENVIRONMENT.loader,
            ]
        )
    )
    return environment.get_template("email/example.html"), environment.get_template(
        "email/example.txt"
    )
