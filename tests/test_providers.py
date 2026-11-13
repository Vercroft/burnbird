from decimal import Decimal

import pytest

from burnbird.providers import ProviderTableError, load_table, parse_table


def _row(provider: str, row_id: str) -> tuple[Decimal | None, Decimal | None]:
    runner = next(r for r in load_table().runners if (r.provider, r.id) == (provider, row_id))
    return runner.usd_per_minute, runner.weight


def test_the_shipped_table_loads() -> None:
    table = load_table()
    assert [provider.id for provider in table.providers] == ["github", "blacksmith", "self-hosted"]
    assert table.runners


def test_github_enterprise_allowance_matches_its_documented_minutes() -> None:
    # "$300 USD covers 50,000 minutes on a standard Linux runner at $0.006 USD per minute."
    github = next(p for p in load_table().providers if p.id == "github")
    enterprise = next(a for a in github.allowances if a.plan == "GitHub Enterprise Cloud")
    price, _ = _row("github", "actions_linux")
    assert price is not None
    assert enterprise.amount / price == 50_000


@pytest.mark.parametrize(
    ("row_id", "minutes", "units"),
    [
        # Blacksmith's own worked examples, in x64 2-vCPU minutes.
        ("linux-x64-4vcpu", 10, 20),
        ("linux-arm-4vcpu", 10, Decimal("12.5")),
        ("windows-x64-4vcpu", 10, 40),
        ("macos-6vcpu", 10, 200),
        ("macos-12vcpu", 10, 400),
    ],
)
def test_blacksmith_weights_match_its_worked_examples(
    row_id: str, minutes: int, units: Decimal
) -> None:
    _, weight = _row("blacksmith", row_id)
    assert weight is not None
    assert weight * minutes == units


VALID = """
[[provider]]
id = "github"
name = "GitHub-hosted"
rounding = "job-up-to-minute"
estimate = false
sources = ["https://docs.example.com/billing"]
read = "2026-11-13"

[[provider.allowance]]
plan = "Free"
amount = "12"
unit = "usd"
period = "billing-cycle"

[[provider]]
id = "blacksmith"
name = "Blacksmith"
rounding = "job-up-to-minute"
estimate = true
sources = ["https://docs.example.com/blacksmith"]
read = "2026-11-13"

[[runner]]
provider = "github"
id = "actions_linux"
name = "Linux 2-core"
kind = "standard"
labels = ["ubuntu-latest"]
usd_per_minute = "0.006"
sources = ["https://docs.example.com/pricing"]
read = "2026-11-12"

[[runner]]
provider = "blacksmith"
id = "linux-x64-2vcpu"
name = "Ubuntu x64 2 vCPU"
labels = ["blacksmith-2vcpu-ubuntu-2404"]
weight = "1"
sources = ["https://docs.example.com/blacksmith"]
read = "2026-11-13"
"""

GITHUB_ROW = "runner github/actions_linux"
BLACKSMITH_ROW = "runner blacksmith/linux-x64-2vcpu"
DUPLICATE_RUNNER = VALID.split("[[runner]]")[1]


def test_the_valid_example_parses() -> None:
    table = parse_table(VALID)
    assert [(r.provider, r.id) for r in table.runners] == [
        ("github", "actions_linux"),
        ("blacksmith", "linux-x64-2vcpu"),
    ]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        pytest.param('id = "github"\n', 'id = "github"\n[', "table: not TOML", id="not-toml"),
        pytest.param(
            'name = "Linux 2-core"\n', "", f"{GITHUB_ROW}: missing name", id="missing-key"
        ),
        pytest.param(
            'usd_per_minute = "0.006"',
            'usd_per_minute = "0.006"\nprice = "1"',
            f"{GITHUB_ROW}: unknown key price",
            id="unknown-key",
        ),
        pytest.param(
            'usd_per_minute = "0.006"',
            "usd_per_minute = 0.006",
            f"{GITHUB_ROW}: usd_per_minute must be a non-empty string",
            id="money-as-float",
        ),
        pytest.param(
            'labels = ["ubuntu-latest"]',
            'labels = "ubuntu-latest"',
            f"{GITHUB_ROW}: labels must be a list",
            id="labels-not-a-list",
        ),
        pytest.param(
            "estimate = false",
            'estimate = "no"',
            "provider github: estimate must be true or false",
            id="estimate-not-bool",
        ),
        pytest.param(
            '"0.006"',
            '"abc"',
            f"{GITHUB_ROW}: usd_per_minute 'abc' is not a finite",
            id="not-decimal",
        ),
        pytest.param(
            '"0.006"', '"-0.006"', f"{GITHUB_ROW}: usd_per_minute '-0.006' is not", id="negative"
        ),
        pytest.param('"0.006"', '"NaN"', f"{GITHUB_ROW}: usd_per_minute 'NaN' is not", id="nan"),
        pytest.param(
            'weight = "1"', 'weight = "Infinity"', f"{BLACKSMITH_ROW}: weight 'Infinity'", id="inf"
        ),
        pytest.param(
            'amount = "12"',
            'amount = "twelve"',
            "provider github allowance 1: amount 'twelve' is not",
            id="allowance-amount",
        ),
        pytest.param(
            'usd_per_minute = "0.006"',
            'usd_per_minute = "0.006"\nweight = "1"',
            f"{GITHUB_ROW}: needs exactly one of usd_per_minute and weight",
            id="price-and-weight",
        ),
        pytest.param(
            'usd_per_minute = "0.006"\n',
            "",
            f"{GITHUB_ROW}: needs exactly one of usd_per_minute and weight",
            id="neither",
        ),
        pytest.param(
            'provider = "github"\nid = "actions_linux"',
            'provider = "gitlab"\nid = "actions_linux"',
            "runner gitlab/actions_linux: provider 'gitlab' is not declared",
            id="undeclared-provider",
        ),
        pytest.param(
            'id = "blacksmith"',
            'id = "github"',
            "provider github: duplicate",
            id="duplicate-provider",
        ),
        pytest.param(
            '[[runner]]\nprovider = "blacksmith"',
            f'[[runner]]{DUPLICATE_RUNNER}[[runner]]\nprovider = "blacksmith"',
            f"{GITHUB_ROW}: duplicate",
            id="duplicate-runner",
        ),
        pytest.param(
            '"https://docs.example.com/pricing"',
            '"http://docs.example.com/pricing"',
            f"{GITHUB_ROW}: sources must be one or more https:// links",
            id="source-not-https",
        ),
        pytest.param(
            'sources = ["https://docs.example.com/pricing"]',
            "sources = []",
            f"{GITHUB_ROW}: sources must be one or more https:// links",
            id="no-source",
        ),
        pytest.param(
            '"2026-11-12"',
            '"13 Nov 2026"',
            f"{GITHUB_ROW}: read '13 Nov 2026' is not",
            id="read-format",
        ),
        pytest.param(
            '"2026-11-12"',
            '"2026-02-30"',
            f"{GITHUB_ROW}: read '2026-02-30' is not",
            id="read-no-day",
        ),
        pytest.param(
            'rounding = "job-up-to-minute"\nestimate = false',
            'rounding = "per-second"\nestimate = false',
            "provider github: rounding 'per-second' is not one of job-up-to-minute, none",
            id="unknown-rounding",
        ),
        pytest.param(
            'kind = "standard"\n',
            "",
            f"{GITHUB_ROW}: kind is required on github rows and allowed only there",
            id="kind-missing",
        ),
        pytest.param(
            'weight = "1"',
            'weight = "1"\nkind = "standard"',
            f"{BLACKSMITH_ROW}: kind is required on github rows and allowed only there",
            id="kind-elsewhere",
        ),
        pytest.param(
            'kind = "standard"',
            'kind = "medium"',
            f"{GITHUB_ROW}: kind 'medium' is not one of standard, larger",
            id="unknown-kind",
        ),
    ],
)
def test_an_invalid_table_is_refused_naming_the_row(old: str, new: str, message: str) -> None:
    assert VALID.count(old) == 1, old
    with pytest.raises(ProviderTableError) as error:
        parse_table(VALID.replace(old, new))
    assert str(error.value).startswith(message)
