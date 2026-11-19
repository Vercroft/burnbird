"""The four run-out warnings, worded and rendered by the real templates through the real outbox.

Invented account, repository and jobs only; the provider is a recording fake and GitHub a mock
transport, so no email and no request leaves a test. Each expected number is worked out by hand in
a comment, never by the code under test.
"""

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, override

import httpx2
import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from burnbird.billing.forecast import WarningKind, forecast
from burnbird.billing.providers import Allowance
from burnbird.billing.usage import AllowanceState
from burnbird.email import outbox, run_out
from burnbird.email.message import Message
from burnbird.email.outbox import Mail
from burnbird.github.client import GithubApp
from burnbird.models import (
    Account,
    EmailPreference,
    GithubUser,
    Installation,
    Job,
    Repository,
    UserEmail,
    UserInstallation,
)
from tests.email.examples import RecordingSender

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
ORIGIN = "https://burnbird.example.invalid"
PERSON, INSTALLATION, REPOSITORY = 7701, 8701, 9701
# Read two days ago: warnings continue 30 days after it, to 15 December.
READ_AT = NOW - timedelta(days=2)
ESTIMATE = "Burnbird's count is an estimate: Blacksmith does not publish how it rounds."
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
type Answer = httpx2.Response | Exception


@pytest.fixture
def person(session: Session) -> Session:
    """One person with their own account, installed long ago, an address and warnings on."""
    session.add(GithubUser(id=PERSON, login="example-person", last_sign_in_at=READ_AT))
    session.add(Account(id=PERSON, login="example-person", type="User", github_plan="GitHub Free"))
    session.flush()
    session.add(
        Installation(
            id=INSTALLATION, account_id=PERSON, created_at=datetime(2026, 9, 1, tzinfo=UTC)
        )
    )
    session.flush()
    session.add_all(
        [
            UserInstallation(user_id=PERSON, installation_id=INSTALLATION),
            UserEmail(user_id=PERSON, address="p" + "@" + "example.invalid", read_at=READ_AT),
            EmailPreference(
                user_id=PERSON, kind="usage_warnings", enabled=True, updated_at=READ_AT
            ),
            Repository(
                id=REPOSITORY,
                installation_id=INSTALLATION,
                name="example-repo",
                private=True,
                backfilled_at=datetime(2026, 9, 2, tzinfo=UTC),
            ),
        ]
    )
    session.flush()
    return session


@pytest.fixture
def permission() -> list[Answer]:
    """GitHub's answers to the permission check, admin unless a test queues another."""
    return []


@pytest.fixture
def sender() -> RecordingSender:
    return RecordingSender()


@pytest.fixture
def mail(rsa_key: RSAKey, sender: RecordingSender, permission: list[Answer]) -> Iterator[Mail]:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            expires = (NOW + timedelta(hours=1)).isoformat()
            return httpx2.Response(
                201, json={"token": "t" + uuid.uuid4().hex, "expires_at": expires}
            )
        answer = (
            permission.pop(0) if permission else httpx2.Response(200, json={"permission": "admin"})
        )
        if isinstance(answer, Exception):
            raise answer
        return answer

    app = GithubApp(
        "Iv23.example",
        rsa_key,
        transport=httpx2.MockTransport(handle),
        clock=lambda: NOW,
        sleep=lambda _: None,
    )
    yield Mail(sender=sender, origin=ORIGIN, github=app, run_out_warnings=True)
    app.close()


def _jobs(session: Session, provider: str, per_day: dict[date, int]) -> None:
    """Store one job a day of the given minutes; GitHub's cost $0.006 a minute, Blacksmith's 1."""
    runner, rate = (
        ("actions_linux", Decimal("0.006"))
        if provider == "github"
        else ("linux-x64-2vcpu", Decimal(1))
    )
    for day, minutes in per_day.items():
        completed = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
        job_id = REPOSITORY * 10_000 + day.toordinal() % 1000 * 10 + (provider == "github")
        session.add(
            Job(
                id=job_id,
                run_attempt=1,
                run_id=job_id,
                repository_id=REPOSITORY,
                labels=["x"],
                created_at=completed,
                started_at=completed - timedelta(minutes=minutes),
                completed_at=completed,
                provider=provider,
                runner_class=runner,
                billed_minutes=Decimal(minutes),
                amount=minutes * rate,
            )
        )
    session.flush()


def _send(
    session: Session,
    sessions: sessionmaker[Session],
    mail: Mail,
    kind: str,
    provider: str = "blacksmith",
    *,
    key: str = "example",
    now: datetime = NOW,
) -> Message:
    runner = "actions_linux" if provider == "github" else "linux-x64-2vcpu"
    params: dict[str, Any] = {
        "provider": provider,
        "kind": kind,
        "cycle_start": "2026-11-01",
        "repository_id": REPOSITORY,
        "runner_class": runner,
    }
    assert outbox.enqueue(
        session,
        user_id=PERSON,
        account_id=PERSON,
        kind="usage_warnings",
        template="run_out_warning",
        dedupe_key=f"{key}:{kind}:{provider}",
        params=params,
        now=now,
        expires_at=now + timedelta(days=1),
    )
    assert isinstance(mail.sender, RecordingSender)
    before = len(mail.sender.sent)
    assert outbox.run(sessions, mail, now) == 1
    assert len(mail.sender.sent) == before + 1, "the warning was not sent"
    return mail.sender.sent[-1]


def _body(message: Message) -> str:
    """The text part above the layout's own lines."""
    return message.text.split("\n\n--\n")[0]


def _days(first: int, last: int, minutes: int) -> dict[date, int]:
    return {date(2026, 11, d): minutes for d in range(first, last + 1)}


TOP = "Most used this cycle: example-repo on"
DETAILS = f"See the details: {ORIGIN}/"


def test_each_kind_and_provider_renders_exactly(
    person: Session, sessions: sessionmaker[Session], mail: Mail
) -> None:
    # GitHub Free, $12: 190 minutes a day from 10 to 16 November at $0.006 is $1.14 a day, $7.98
    # used; $4.02 left at $1.14 a day is 3.5 days from 17 November, rounded down: 20 November.
    _jobs(person, "github", _days(10, 16, 190))
    github = _send(person, sessions, mail, "runs-out-before-reset", "github")
    assert github.subject == (
        "example-person: included GitHub Actions usage may run out on 20 November 2026 (UTC)"
    )
    assert _body(github) == (
        "At its pace over the last 7 days, example-person uses up its included GitHub Actions "
        "usage on 20 November 2026 (UTC), before it resets on 1 December 2026 (UTC). After that, "
        "GitHub bills further use or stops jobs, as your budget settings decide. "
        f"{TOP} Linux 2-core (x64). {DETAILS}"
    )
    # Blacksmith, 3,000: 170 a day from 1 to 16 November is 2,720 used, 90.7%.
    _jobs(person, "blacksmith", _days(1, 16, 170))
    used = _send(person, sessions, mail, "used-90")
    assert used.subject == "example-person has used at least 90% of its free Blacksmith minutes"
    assert _body(used) == (
        "example-person has used at least 90% of its 3,000 free Blacksmith minutes this cycle, "
        "about 2,720 as of 17 November 2026 (UTC). They reset on 1 December 2026 (UTC). "
        f"{TOP} Ubuntu x64 2 vCPU. {ESTIMATE} {DETAILS}"
    )
    # 280 left at a pace of 170 a day is 1.6 days, rounded down to 1: 18 November.
    runs_out = _send(person, sessions, mail, "runs-out-before-reset")
    assert (
        runs_out.subject
        == "example-person: Blacksmith billing may start on 18 November 2026 (UTC)"
    )
    assert _body(runs_out) == (
        "At its pace over the last 7 days, example-person uses up its 3,000 free Blacksmith "
        "minutes on 18 November 2026 (UTC), before they reset on 1 December 2026 (UTC). "
        "Blacksmith keeps running after that, so billing starts that day. "
        f"{TOP} Ubuntu x64 2 vCPU. {ESTIMATE} {DETAILS}"
    )


def test_a_used_up_allowance_and_the_75_percent_warning_render_exactly(
    person: Session, sessions: sessionmaker[Session], mail: Mail
) -> None:
    # 150 a day from 1 to 16 November is 2,400 of 3,000: 80%.
    _jobs(person, "blacksmith", _days(1, 16, 150))
    used = _send(person, sessions, mail, "used-75")
    assert used.subject == "example-person has used at least 75% of its free Blacksmith minutes"
    assert _body(used) == (
        "example-person has used at least 75% of its 3,000 free Blacksmith minutes this cycle, "
        "about 2,400 as of 17 November 2026 (UTC). They reset on 1 December 2026 (UTC). "
        f"{TOP} Ubuntu x64 2 vCPU. {ESTIMATE} {DETAILS}"
    )
    # 50 more a day from 1 to 16 November: 200 a day reaches 3,000 on the 15th.
    person.execute(update(Job).values(amount=Decimal(200), billed_minutes=Decimal(200)))
    ran_out = _send(person, sessions, mail, "ran-out", key="later")
    assert ran_out.subject == "example-person has used all its free Blacksmith minutes"
    assert _body(ran_out) == (
        "example-person used up its 3,000 free Blacksmith minutes on 15 November 2026 (UTC). "
        "Blacksmith keeps running, so billing started that day. The free minutes reset on "
        f"1 December 2026 (UTC). {TOP} Ubuntu x64 2 vCPU. {ESTIMATE} {DETAILS}"
    )


def test_the_html_part_has_the_same_sentences(
    person: Session, sessions: sessionmaker[Session], mail: Mail
) -> None:
    _jobs(person, "blacksmith", _days(1, 16, 150))
    html = _send(person, sessions, mail, "used-75").html
    content = html.split("<hr>")[0]
    assert (
        "<p>example-person has used at least 75% of its 3,000 free Blacksmith minutes this "
        "cycle, about 2,400 as of 17 November 2026 (UTC). They reset on 1 December 2026 (UTC).</p>"
    ) in content
    assert "<p>Most used this cycle: example-repo on Ubuntu x64 2 vCPU.</p>" in content
    assert f"<p>{ESTIMATE}</p>" in content
    assert f'<p>See the details: <a href="{ORIGIN}/">{ORIGIN}/</a></p>' in content


def test_without_the_repository_the_top_line_points_to_the_page(
    person: Session, sessions: sessionmaker[Session], mail: Mail, permission: list[Answer]
) -> None:
    permission.append(httpx2.Response(404))
    _jobs(person, "blacksmith", _days(1, 16, 150))
    message = _send(person, sessions, mail, "used-75")
    assert "Most used this cycle: Ubuntu x64 2 vCPU. See which repository on the page." in (
        message.text
    )
    assert "example-repo" not in message.text
    assert "example-repo" not in message.html


def test_every_date_says_utc(person: Session, sessions: sessionmaker[Session], mail: Mail) -> None:
    _jobs(person, "github", _days(10, 16, 190))
    _jobs(person, "blacksmith", _days(1, 16, 170))
    messages = [
        _send(person, sessions, mail, "runs-out-before-reset", "github"),
        _send(person, sessions, mail, "used-90"),
        _send(person, sessions, mail, "runs-out-before-reset"),
    ]
    for message in messages:
        found = 0
        for part in (message.subject, message.text, message.html):
            dates = re.findall(rf"\d{{1,2}} (?:{MONTHS}) \d{{4}}( \(UTC\))?", part)
            found += len(dates)
            assert all(dates), part
            assert not re.search(r"\b0\d (?:" + MONTHS + ")", part)
            assert not re.search(r"\d{4}-\d{2}-\d{2}", part)
        assert found >= 3


def test_no_amount_in_dollars(
    person: Session, sessions: sessionmaker[Session], mail: Mail
) -> None:
    _jobs(person, "github", _days(10, 16, 190))
    message = _send(person, sessions, mail, "runs-out-before-reset", "github")
    for part in (message.subject, _body(message), message.html.split("<hr>")[0]):
        assert "$" not in part
        assert "USD" not in part
        assert "dollar" not in part.lower()
        # Neither the allowance (12), what is used (7.98) nor what is left (4.02).
        assert not re.search(r"\b(?:12|7\.98|4\.02)\b", part)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.hrefs.extend(value or "" for name, value in attrs if tag == "a" and name == "href")


def test_the_only_content_link_is_the_dashboard(
    person: Session, sessions: sessionmaker[Session], mail: Mail
) -> None:
    _jobs(person, "blacksmith", _days(1, 16, 200))
    for kind in ("used-90", "ran-out"):
        message = _send(person, sessions, mail, kind)
        links = _Links()
        links.feed(message.html.split("<hr>")[0])
        assert links.hrefs == [f"{ORIGIN}/"]
        assert re.findall(r"https://\S+", _body(message)) == [f"{ORIGIN}/"]


def test_the_warnings_until_date_moves_with_a_sign_in(
    person: Session, sessions: sessionmaker[Session], mail: Mail
) -> None:
    _jobs(person, "blacksmith", _days(1, 16, 150))
    first = _send(person, sessions, mail, "used-75")
    line = "Warnings continue until 15 December 2026 (UTC). Sign in to keep them."
    assert line in first.text
    assert f"<p>{line}</p>" in first.html
    # A sign-in on 20 November reads the address again: 30 days later is 20 December.
    person.execute(update(UserEmail).values(read_at=datetime(2026, 11, 20, 8, tzinfo=UTC)))
    # Another used-75 warning: 2,400 of 3,000 is still 80%.
    later = _send(
        person, sessions, mail, "used-75", key="later", now=datetime(2026, 11, 20, 9, tzinfo=UTC)
    )
    assert "Warnings continue until 20 December 2026 (UTC). Sign in to keep them." in later.text


def test_html_escapes_every_value(
    person: Session, sessions: sessionmaker[Session], mail: Mail
) -> None:
    # GitHub allows neither in a name; planted to show the HTML part escapes whatever it is given.
    person.execute(update(Repository).values(name="<b>repo&"))
    person.execute(update(Account).values(login="<i>acct"))
    _jobs(person, "blacksmith", _days(1, 16, 150))
    message = _send(person, sessions, mail, "used-75")
    assert "<b>" not in message.html
    assert "<i>" not in message.html
    assert "&lt;b&gt;repo&amp; on Ubuntu" in message.html
    assert "<title>&lt;i&gt;acct has used" in message.html
    assert "<p>&lt;i&gt;acct has used" in message.html
    # The text part is not HTML: it keeps the names as they are.
    assert "<b>repo& on Ubuntu" in message.text


def test_no_subject_holds_a_line_break(
    person: Session, sessions: sessionmaker[Session], mail: Mail
) -> None:
    person.execute(update(Account).values(login="example\r\nBcc: someone\nelse"))
    _jobs(person, "blacksmith", _days(1, 16, 200))
    for kind in ("used-90", "ran-out"):
        message = _send(person, sessions, mail, kind)
        assert "\r" not in message.subject
        assert "\n" not in message.subject
        assert message.subject.startswith("example Bcc: someone else ")


@pytest.mark.parametrize(
    ("day", "words"),
    [
        (date(2026, 1, 1), "1 January 2026 (UTC)"),
        (date(2026, 2, 9), "9 February 2026 (UTC)"),
        (date(2026, 3, 31), "31 March 2026 (UTC)"),
        (date(2026, 4, 5), "5 April 2026 (UTC)"),
        (date(2026, 5, 5), "5 May 2026 (UTC)"),
        (date(2026, 6, 5), "5 June 2026 (UTC)"),
        (date(2026, 7, 5), "5 July 2026 (UTC)"),
        (date(2026, 8, 5), "5 August 2026 (UTC)"),
        (date(2026, 9, 5), "5 September 2026 (UTC)"),
        (date(2026, 10, 5), "5 October 2026 (UTC)"),
        (date(2026, 11, 17), "17 November 2026 (UTC)"),
        (date(2026, 12, 1), "1 December 2026 (UTC)"),
    ],
)
def test_a_date_is_the_day_without_a_leading_zero_and_the_english_month(
    day: date, words: str
) -> None:
    assert run_out.utc_date(day) == words


def test_month_names_come_from_the_fixed_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_out, "MONTHS", tuple(f"M{n}" for n in range(1, 13)))
    assert run_out.utc_date(date(2026, 11, 7)) == "7 M11 2026 (UTC)"


@pytest.mark.parametrize(
    ("provider", "kind"),
    [("github", "used-90"), ("github", "ran-out"), ("self-hosted", "used-75")],
)
def test_a_warning_with_no_wording_of_its_own_is_refused(provider: str, kind: str) -> None:
    # The table may one day list more kinds or providers; until they are worded, none is sent in
    # another provider's words.
    params = run_out.WarningParams(provider, WarningKind(kind), date(2026, 11, 1), None, None)
    state = AllowanceState(
        provider,
        Allowance("example", Decimal(100), "usd", "billing-cycle"),
        date(2026, 9, 1),
        forecast({}, Decimal(100), 1, NOW.date(), complete_from=date(2026, 9, 1)),
    )
    with pytest.raises(ValueError, match="no wording"):
        run_out.context(
            run_out.Checked(params, state, None), account="a", repository=None, now=NOW
        )


@pytest.mark.parametrize(
    ("amount", "words"),
    [
        ("0", "0"),
        ("999.9", "999"),
        ("2340", "2,340"),
        ("2340.99", "2,340"),
        ("1234567", "1,234,567"),
    ],
)
def test_minutes_are_whole_with_a_thousands_separator(amount: str, words: str) -> None:
    assert run_out.whole(Decimal(amount)) == words
