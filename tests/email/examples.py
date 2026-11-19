"""Example email templates and a recording sender for the tests; no real email is ever sent."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from jinja2 import ChoiceLoader, DictLoader, Environment, Template

from burnbird.email.message import Message
from burnbird.email.sender import Result
from burnbird.models import Job
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


# The top runner and repository, when the send names them.
_TOP = (
    "{% if runner %} on {{ runner }}{% endif %}{% if repository %} in {{ repository }}{% endif %}"
)


def example_environment() -> Environment:
    """The environment with an example ``run_out_warning`` email, for tests of the sending."""
    assert ENVIRONMENT.loader is not None
    return ENVIRONMENT.overlay(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        "email/run_out_warning.subject.txt": "Example {{ kind }} warning",
                        "email/run_out_warning.txt": "{% extends 'email/layout.txt' %}"
                        "{% block body %}Example {{ kind }} warning"
                        f"{_TOP}{{% endblock %}}",
                        "email/run_out_warning.html": "{% extends 'email/layout.html' %}"
                        "{% block body %}<p>Example {{ kind }} warning"
                        f"{_TOP}</p>{{% endblock %}}",
                    }
                ),
                ENVIRONMENT.loader,
            ]
        )
    )


def example_params(now: datetime, kind: str = "used-75") -> dict[str, Any]:
    """A Blacksmith run-out warning's params for the cycle that holds ``now`` (cycle day 1)."""
    return {
        "provider": "blacksmith",
        "kind": kind,
        "cycle_start": now.date().replace(day=1).isoformat(),
        "repository_id": None,
        "runner_class": None,
    }


def example_usage(job_id: int, repository_id: int, at: datetime, minutes: int = 2400) -> Job:
    """A Blacksmith job of ``minutes`` completed at ``at``: 2,400 is 80% of the free 3,000.

    So a used-75 warning for its account is still true at send.
    """
    return Job(
        id=job_id,
        run_attempt=1,
        run_id=job_id,
        repository_id=repository_id,
        labels=["blacksmith-2vcpu-ubuntu-2404"],
        created_at=at - timedelta(minutes=minutes),
        started_at=at - timedelta(minutes=minutes),
        completed_at=at,
        provider="blacksmith",
        runner_class="linux-x64-2vcpu",
        billed_minutes=Decimal(minutes),
        amount=Decimal(minutes),
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


def webhook_event(kind: str, reference: str, address: str) -> dict[str, Any]:
    """A provider webhook body in the documented shape, with invented values only.

    ``kind`` is the event's object (``hardbounce``, ``softbounce``, ``fbl_complaint``,
    ``email_open``); the reported address goes where that event names it.
    """
    at = "2026-11-17T09:00:00Z"
    if kind in ("hardbounce", "softbounce"):
        details: dict[str, Any] = {
            "reason": "example-reason",
            "bounced_recipient": address,
            "time": at,
            "diagnostic_message": "example-diagnostic",
        }
    elif kind == "fbl_complaint":
        details = {"fblFrom": address, "returnPath": "bounce", "from": "sender", "to": address}
    else:
        details = {"time": at}
    return {
        "event_name": [kind],
        "event_message": [
            {
                "email_info": {
                    "client_reference": reference,
                    "email_reference": "example-trace",
                    "is_smtp_trigger": False,
                    "subject": "Example",
                    "from": {"address": "sender", "name": "Burnbird"},
                    "to": [{"email_address": {"address": address, "name": ""}}],
                    "processed_time": at,
                    "object": "email",
                },
                "event_data": [{"details": [details], "object": kind}],
                "request_id": "example-request",
            }
        ],
        "mailagent_key": "example-agent",
        "webhook_request_id": "example-webhook-request",
    }
