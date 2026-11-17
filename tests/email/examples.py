"""Example email templates and a recording sender for the tests; no real email is ever sent."""

from collections.abc import Callable
from dataclasses import dataclass, field

from jinja2 import ChoiceLoader, DictLoader, Environment, Template

from burnbird.email.message import Message
from burnbird.email.sender import Result
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


def example_environment() -> Environment:
    """The environment with an example ``run_out_warning`` email, until the real one exists."""
    assert ENVIRONMENT.loader is not None
    return ENVIRONMENT.overlay(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        "email/run_out_warning.subject.txt": "Example warning for {{ account }}",
                        "email/run_out_warning.txt": "{% extends 'email/layout.txt' %}"
                        "{% block body %}Example warning for {{ account }}{% endblock %}",
                        "email/run_out_warning.html": "{% extends 'email/layout.html' %}"
                        "{% block body %}<p>Example warning for {{ account }}</p>{% endblock %}",
                    }
                ),
                ENVIRONMENT.loader,
            ]
        )
    )


@dataclass
class RecordingSender:
    """A sender that records each message and answers with the next result (sent by default)."""

    results: list[Result] = field(default_factory=list)
    sent: list[Message] = field(default_factory=list)
    # Called on each send, before it answers: a race test records the order of events with it.
    on_send: Callable[[], None] | None = None

    def send(self, message: Message) -> Result:
        """Record the message and answer."""
        self.sent.append(message)
        if self.on_send is not None:
            self.on_send()
        return self.results.pop(0) if self.results else Result("sent", "http:200", "req-example")
