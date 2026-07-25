#!/usr/bin/env python3
"""Enforce the §3.4 banned list by parsing the code, not by grepping it.

Section 3.4 names two constructions that "fail silently and look identical to
the correct choice". They are worth a build-breaking check — but a text search
cannot tell a call from the docstring that explains why the call is banned, and
this repository is full of the latter. The first version of this check failed
CI on `backends.py`'s own explanation of the rule.

So: parse, walk, and look at actual call sites.

Run standalone (`python scripts/check_portability_contract.py`) or via CI.
Exits non-zero with a specific file:line on violation.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

#: Vendor-hosted memory. Same ergonomics as StoreBackend, opposite risk.
BANNED_OUTRIGHT = {"ContextHubBackend"}

#: Binds to whatever store the runtime supplies at call time, which is exactly
#: the coupling P1 forbids. Must be constructed with an explicit, non-None store.
REQUIRES_EXPLICIT_STORE = {"StoreBackend"}


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def violations(tree: ast.AST, path: Path) -> Iterator[str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name is None:
            continue

        if name in BANNED_OUTRIGHT:
            yield (
                f"{path}:{node.lineno}: §3.4 bans {name} — vendor-hosted memory. "
                f"Construct a store we chose and pass it explicitly."
            )
            continue

        if name not in REQUIRES_EXPLICIT_STORE:
            continue

        store = next((kw for kw in node.keywords if kw.arg == "store"), None)
        if store is None:
            yield (
                f"{path}:{node.lineno}: §3.4 bans {name} without an explicit "
                f"store= — it resolves the store from the runtime at call time."
            )
        elif isinstance(store.value, ast.Constant) and store.value.value is None:
            yield (
                f"{path}:{node.lineno}: §3.4 bans {name}(store=None) by name — "
                f"that is the construction, not a placeholder."
            )


def check(root: Path = SRC) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - a syntax error fails the build anyway
            found.append(f"{path}: could not parse: {exc}")
            continue
        found.extend(violations(tree, path.relative_to(root.parent.parent)))
    return found


def main() -> int:
    found = check()
    if found:
        for line in found:
            print(f"::error::{line}" if "--github" in sys.argv else line, file=sys.stderr)
        return 1
    print("ok: no §3.4 banned constructions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
