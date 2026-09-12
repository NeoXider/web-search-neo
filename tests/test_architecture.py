"""Architecture checks are enforced and their failure paths are exercised."""

from pathlib import Path

import pytest

from scripts.check_module_size import boundary_errors, check_boundaries, check_sizes


ROOT = Path(__file__).resolve().parents[1]


def test_production_module_sizes():
    errors, _ = check_sizes(ROOT)
    assert not errors, "\n".join(errors)


def test_leaf_dependency_boundaries():
    assert not (errors := check_boundaries(ROOT)), "\n".join(errors)


@pytest.mark.parametrize("suffix", ["py", "js"])
def test_size_checker_rejects_new_oversize_modules(tmp_path, suffix):
    path = tmp_path / f"new.{suffix}"
    path.write_text("# line\n" * 801, encoding="utf-8")
    errors, _ = check_sizes(tmp_path, ratchets={})
    assert len(errors) == 1 and "maximum 800" in errors[0]


def test_legacy_ratchet_rejects_growth(tmp_path):
    (tmp_path / "legacy.py").write_text("# line\n" * 902, encoding="utf-8")
    errors, _ = check_sizes(tmp_path, ratchets={"legacy.py": 901})
    assert len(errors) == 1 and "maximum 901" in errors[0]


def test_soft_limit_warns_without_failure(tmp_path):
    (tmp_path / "module.py").write_text("# line\n" * 601, encoding="utf-8")
    errors, warnings = check_sizes(tmp_path, ratchets={})
    assert not errors and len(warnings) == 1


@pytest.mark.parametrize("source", [
    "from .. import browser_tools", "from ..main import web_info",
    "from web_search_neo import browser_tools", "import web_search_neo.main",
    "import web_search_neo", "from ...web_search_neo import main",
    "import requests", "from ..cdp import context",
])
def test_forbidden_imports_including_relative_are_rejected(source):
    assert boundary_errors(source, "web_search_neo/sessions/state.py")


@pytest.mark.parametrize("source", [
    "from ..sessions import state", "from web_search_neo.sessions import state",
    "from . import helpers", "from .helpers import useful", "import json",
    "from ..key_table import KEY_TABLE",
])
def test_allowed_dependencies_and_own_modules(source):
    assert not boundary_errors(source, "web_search_neo/actions/fill.py")
