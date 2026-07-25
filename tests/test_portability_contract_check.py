"""The §3.4 checker must catch real violations and ignore prose about them.

The first version of this check was a grep, and it failed CI on `backends.py`'s
own docstring explaining the ban. A checker that cannot distinguish a call from
a sentence about the call is a checker people disable.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_portability_contract.py"

sys.path.insert(0, str(SCRIPT.parent))
from check_portability_contract import check, violations  # noqa: E402


def _violations(source: str) -> list[str]:
    return list(violations(ast.parse(source), Path("probe.py")))


def test_the_real_source_tree_is_clean():
    assert check() == []


def test_the_script_exits_zero_on_this_repo():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_prose_naming_the_banned_pattern_is_not_a_violation():
    """This is the false positive that broke the first CI run."""
    source = '''
"""§3.4 lists `StoreBackend(store=None)` by name because it fails silently.

Never write ContextHubBackend(...) either.
"""
# StoreBackend() in a comment is also not a call.
x = 1
'''
    assert _violations(source) == []


def test_a_store_backend_without_a_store_is_caught():
    found = _violations("b = StoreBackend(namespace=ns)")
    assert len(found) == 1
    assert "without an explicit store=" in found[0]


def test_an_explicitly_none_store_is_caught():
    found = _violations("b = StoreBackend(store=None, namespace=ns)")
    assert len(found) == 1
    assert "store=None" in found[0]


def test_a_bare_store_backend_is_caught():
    assert len(_violations("b = StoreBackend()")) == 1


def test_context_hub_backend_is_caught_however_it_is_reached():
    assert len(_violations("b = ContextHubBackend('id')")) == 1
    assert len(_violations("b = backends.ContextHubBackend('id')")) == 1


def test_an_explicit_store_is_allowed():
    assert _violations("b = StoreBackend(store=store, namespace=ns)") == []
    assert _violations("b = StoreBackend(store=GitSkillStore(p))") == []


def test_line_numbers_point_at_the_call():
    found = _violations("\n\n\nb = StoreBackend()\n")
    assert ":4:" in found[0]


def test_a_planted_violation_fails_the_real_check(tmp_path: Path):
    """End to end: the checker walks a tree and reports what it finds."""
    pkg = tmp_path / "src" / "planted"
    pkg.mkdir(parents=True)
    (pkg / "ok.py").write_text("x = StoreBackend(store=s)\n", encoding="utf-8")
    (pkg / "bad.py").write_text("y = ContextHubBackend('leak')\n", encoding="utf-8")

    found = check(tmp_path / "src")
    assert len(found) == 1
    assert "bad.py" in found[0]
    assert "ContextHubBackend" in found[0]
