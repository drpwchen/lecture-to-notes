"""pyproject.toml must not drift from the requirements files or the changelog."""
import os
import re
import tomllib

from conftest import REPO


def _reqs(name):
    out = []
    with open(os.path.join(REPO, name), encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def _pyproject():
    with open(os.path.join(REPO, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)["project"]


def test_dependencies_match_requirements_txt():
    assert _pyproject()["dependencies"] == _reqs("requirements.txt")


def test_optional_dependencies_match_requirements_optional_txt():
    assert _pyproject()["optional-dependencies"]["optional"] == _reqs("requirements-optional.txt")


def test_version_is_the_latest_changelog_release():
    with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as fh:
        m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", fh.read(), re.M)
    assert m and _pyproject()["version"] == m.group(1)
