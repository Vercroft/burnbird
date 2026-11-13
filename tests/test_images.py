"""Every workflow image is pinned by digest, classified, and its major held where it must be.

A database, runtime or base image moves to a new major only in a planned change, so the Dependabot
entry that reads the workflows ignores its majors. A new image fails here until someone decides
which kind it is.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

# Image name: True when its majors are held back (a database, runtime or base image).
HELD = {"postgres": True}

IMAGE_LINE = re.compile(r"^\s*image:\s*(\S+)\s*$", re.MULTILINE)
MAJOR = re.escape('"version-update:semver-major"')
PINNED = re.compile(r"^(?P<name>[a-z0-9][a-z0-9._/-]*):[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def _images() -> list[str]:
    return [
        image
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        for image in IMAGE_LINE.findall(workflow.read_text())
    ]


def _workflows_entry() -> str:
    """The text of the Dependabot docker entry that reads the workflows folder."""
    entries = re.split(r"(?m)^  - ", DEPENDABOT.read_text())
    matches = [
        entry
        for entry in entries
        if "package-ecosystem: docker" in entry and "directory: /.github/workflows" in entry
    ]
    assert len(matches) == 1, "one docker entry must read /.github/workflows"
    return matches[0]


def test_the_workflows_use_images() -> None:
    assert _images(), "no image found: the pattern no longer reads the workflows"


def test_every_workflow_image_is_pinned_by_tag_and_digest() -> None:
    for image in _images():
        assert PINNED.match(image), f"{image} is not name:tag@sha256:<digest>"


def test_every_workflow_image_is_classified() -> None:
    for image in _images():
        match = PINNED.match(image)
        assert match is not None
        assert match["name"] in HELD, f"decide whether {match['name']}'s majors are held"


def test_held_images_have_their_majors_ignored() -> None:
    entry = _workflows_entry()
    for name, held in HELD.items():
        ignore = rf"- dependency-name: {re.escape(name)}\n\s+update-types: \[{MAJOR}\]"
        ignored = re.search(ignore, entry)
        assert (ignored is not None) == held, f"{name}'s majors"
