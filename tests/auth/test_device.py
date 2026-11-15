"""The device label: client hints first, then the User-Agent, never more than our own words.

The User-Agent strings follow each browser's documented shape; versions are invented.
"""

import pytest

from burnbird.auth.device import MAX_LENGTH, device_label

# Chromium-based browsers write versions as four numbers; built from parts here, so a version is
# never a literal that looks like an IP address (tests/test_public_text.py).
ZEROS = ".0" * 3

AGENTS = {
    "Firefox 140 on Linux, computer": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
    ),
    "Firefox 140 on Windows, computer": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
    ),
    "Chrome 141 on Windows, computer": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/141{ZEROS} Safari/537.36"
    ),
    "Chrome 141 on Linux, computer": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/141{ZEROS} Safari/537.36"
    ),
    "Safari 18 on macOS, computer": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.6 Safari/605.1.15"
    ),
    "Safari 18 on iOS, phone": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1"
    ),
    "Safari 18 on iPadOS, tablet": (
        "Mozilla/5.0 (iPad; CPU OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1"
    ),
    "Firefox 140 on iOS, phone": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) FxiOS/140.0 Mobile/15E148 Safari/605.1.15"
    ),
    "Edge 141 on Windows, computer": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/141{ZEROS} Safari/537.36 Edg/141{ZEROS}"
    ),
    "Samsung Internet 28 on Android, phone": (
        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"SamsungBrowser/28.0 Chrome/130{ZEROS} Mobile Safari/537.36"
    ),
    "Chrome 141 on Android, phone": (
        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/141{ZEROS} Mobile Safari/537.36"
    ),
    "Chrome 141 on Android, tablet": (
        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/141{ZEROS} Safari/537.36"
    ),
    "Chrome 141 on ChromeOS, computer": (
        "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/141{ZEROS} Safari/537.36"
    ),
    "Opera 120 on Windows, computer": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/135{ZEROS} Safari/537.36 OPR/120{ZEROS}"
    ),
}


@pytest.mark.parametrize(("label", "agent"), AGENTS.items(), ids=AGENTS.keys())
def test_a_user_agent_gives_its_label(label: str, agent: str) -> None:
    assert device_label({"user-agent": agent}) == label


def test_client_hints_come_before_the_user_agent() -> None:
    headers = {
        # A reduced User-Agent says less than the hints do.
        "user-agent": AGENTS["Chrome 141 on Linux, computer"],
        "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="142", "Microsoft Edge";v="142"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    }
    assert device_label(headers) == "Edge 142 on Windows, computer"


@pytest.mark.parametrize(
    ("mobile", "platform", "kind"),
    [("?1", '"Android"', "phone"), ("?0", '"Android"', "tablet"), ("?0", '"macOS"', "computer")],
)
def test_the_mobile_hint_decides_phone_tablet_or_computer(
    mobile: str, platform: str, kind: str
) -> None:
    headers = {
        "sec-ch-ua": '"Google Chrome";v="141", "Chromium";v="141"',
        "sec-ch-ua-platform": platform,
        "sec-ch-ua-mobile": mobile,
    }
    assert device_label(headers).endswith(f", {kind}")


def test_only_the_chromium_brand_names_chromium() -> None:
    headers = {"sec-ch-ua": '"Chromium";v="141", "Not=A?Brand";v="24"'}
    assert device_label(headers).startswith("Chromium 141 on ")


@pytest.mark.parametrize("agent", ["", "curl/8.11.1", "x" * 2000])
def test_an_unknown_agent_reads_unknown_browser(agent: str) -> None:
    assert device_label({"user-agent": agent}) == "Unknown browser on unknown system, computer"


def test_nothing_from_the_headers_but_our_own_words_and_digits_reaches_the_label() -> None:
    planted = "planted-" + "marker"
    headers = {
        "user-agent": f"Mozilla/5.0 ({planted}; Linux x86_64) Firefox/140.0 {planted}",
        "sec-ch-ua": f'"{planted}";v="9", "Google Chrome";v="141"',
        "sec-ch-ua-platform": f'"{planted}"',
        "sec-ch-ua-mobile": planted,
    }
    label = device_label(headers)
    assert planted not in label
    assert label == "Chrome 141 on Linux, computer"
    assert len(label) <= MAX_LENGTH
