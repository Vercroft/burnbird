"""One Python version, stated in every place that names one.

``.python-version`` is the declaration: the exact patch that uv installs, locally and in
CI. mypy checks against its feature release, and ``requires-python`` is the range of that
feature release, not an exact patch: Dependabot's uv job installs the newest patch of the
feature release it reads and then locks, so an exact ``==`` would refuse the next patch.
uv writes the range into ``uv.lock`` as ``==X.Y.*``.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / ".python-version").read_text().strip()


def _feature_release() -> tuple[int, int]:
    major, minor, _patch = (int(part) for part in VERSION.split("."))
    return major, minor


def _requires_python(path: Path) -> str:
    data = tomllib.loads(path.read_text())
    project = data.get("project", data)
    return str(project["requires-python"]).replace(" ", "")


def test_the_declaration_is_an_exact_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION


def test_requires_python_is_the_range_of_the_declared_feature_release() -> None:
    major, minor = _feature_release()
    requires = _requires_python(ROOT / "pyproject.toml")
    assert requires == f">={major}.{minor},<{major}.{minor + 1}", (requires, VERSION)


def test_uv_lock_records_the_same_feature_release() -> None:
    major, minor = _feature_release()
    spellings = {f">={major}.{minor},<{major}.{minor + 1}", f"=={major}.{minor}.*"}
    requires = _requires_python(ROOT / "uv.lock")
    assert requires in spellings, (requires, VERSION)


def test_mypy_checks_against_the_same_feature_release() -> None:
    major, minor = _feature_release()
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["tool"]["mypy"]["python_version"] == f"{major}.{minor}", VERSION
