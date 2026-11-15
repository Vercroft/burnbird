"""A coarse label for the device a session signed in from: "Firefox 140 on Linux, computer".

The label is built only from the names in the tables below and at most four digits of a major
version, so nothing else from the request headers can reach the database or a log: the raw
headers are never stored or logged. The user-agent client hints come first where a browser sends
them; they are low-entropy hints a browser "may" send "by default" (MDN Web Docs, "HTTP Client
hints"). The User-Agent string is read otherwise. A label is for the user to recognise their own
sessions, so a wrong guess costs nothing: anything not known reads "Unknown browser" or "unknown
system".
"""

import re
from collections.abc import Mapping
from typing import Final

MAX_LENGTH: Final = 80
UNKNOWN_BROWSER: Final = "Unknown browser"
UNKNOWN_SYSTEM: Final = "unknown system"

# Client-hint brands we name, in the order checked; "Chromium" only when no other brand is known.
_BRANDS: Final = (
    ("Microsoft Edge", "Edge"),
    ("Opera", "Opera"),
    ("Samsung Internet", "Samsung Internet"),
    ("Google Chrome", "Chrome"),
    ("Chromium", "Chromium"),
)
# User-Agent tokens, in the order checked: Edge, Opera and Samsung Internet also carry
# "Chrome/", and every browser on iOS carries "Safari/".
_TOKENS: Final = (
    ("Edg/", "Edge"),
    ("EdgA/", "Edge"),
    ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("FxiOS/", "Firefox"),
    ("Firefox/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Version/", "Safari"),
)
_PLATFORMS: Final = {
    "Windows": "Windows",
    "macOS": "macOS",
    "Linux": "Linux",
    "Android": "Android",
    "Chrome OS": "ChromeOS",
    "ChromeOS": "ChromeOS",
    "iOS": "iOS",
}
# User-Agent systems, in the order checked: Android carries "Linux", and iOS "like Mac OS X".
_SYSTEMS: Final = (
    ("iPad", "iPadOS"),
    ("iPhone", "iOS"),
    ("iPod", "iOS"),
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("Windows", "Windows"),
    ("Macintosh", "macOS"),
    ("Linux", "Linux"),
)
_BRAND: Final = re.compile(r'"([^"]{1,40})"\s*;\s*v\s*=\s*"(\d{1,4})')
_MAJOR: Final = re.compile(r"(\d{1,4})")


def device_label(headers: Mapping[str, str]) -> str:
    """Return the label for a request's headers (header names in lower case)."""
    agent = headers.get("user-agent", "")
    system = _platform(headers.get("sec-ch-ua-platform")) or _system(agent)
    label = f"{_brand(headers.get('sec-ch-ua')) or _browser(agent)} on {system}, "
    return (label + _kind(agent, headers.get("sec-ch-ua-mobile"), system))[:MAX_LENGTH]


def _brand(hint: str | None) -> str | None:
    if not hint:
        return None
    versions = dict(_BRAND.findall(hint))
    for brand, name in _BRANDS:
        if brand in versions:
            return f"{name} {int(versions[brand])}"
    return None


def _browser(agent: str) -> str:
    for token, name in _TOKENS:
        start = agent.find(token)
        if start < 0 or (name == "Safari" and "Safari/" not in agent):
            continue
        major = _MAJOR.match(agent, start + len(token))
        return f"{name} {int(major[1])}" if major else name
    return UNKNOWN_BROWSER


def _platform(hint: str | None) -> str | None:
    return _PLATFORMS.get(hint.strip().strip('"')) if hint else None


def _system(agent: str) -> str:
    return next((name for token, name in _SYSTEMS if token in agent), UNKNOWN_SYSTEM)


def _kind(agent: str, mobile_hint: str | None, system: str) -> str:
    if mobile_hint in ("?1", "?0"):
        if mobile_hint == "?1":
            return "phone"
        return "tablet" if system == "Android" else "computer"
    if system == "iPadOS" or (system == "Android" and "Mobile" not in agent):
        return "tablet"
    if system in ("iOS", "Android"):
        return "phone"
    return "computer"
