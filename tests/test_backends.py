"""Backend routing, and the two §3.4 bans that fail silently when broken.

`StoreBackend(store=None)` and `ContextHubBackend` both look identical to the
correct choice at the call site. Neither raises, neither logs, and both hand a
system-of-record store to somebody else. The only way to keep them out is to
assert their absence, which is what the source-inspection tests below do.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from deepagents.backends import CompositeBackend, LocalShellBackend, StateBackend, StoreBackend

from agent_platform.harness.backends import (
    MEMORIES_ROUTE,
    SKILLS_ROUTE,
    StoreBackendWithoutStoreError,
    _store_backend,
    build_backend,
)
from agent_platform.harness.skills_store import GitSkillStore

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "agent_platform"


def construction_sites(class_name: str) -> list[str]:
    """Every module in the package that *calls* `class_name`.

    Parsed rather than grepped. A ban you enforce with a regex fires on the
    docstring that explains the ban, which trains people to delete the test.
    """
    sites = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called == class_name:
                sites.add(path.relative_to(PACKAGE_ROOT).as_posix())
    return sorted(sites)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    skill = tmp_path / "web-research"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: web-research\ndescription: How to research a topic\n---\n\nBody.\n"
    )
    return tmp_path


def test_composite_routes_skills_to_the_store_and_everything_else_to_state(
    checkout: Path,
) -> None:
    backend = build_backend(skill_store=GitSkillStore(checkout))

    assert isinstance(backend, CompositeBackend)
    assert isinstance(backend.default, StateBackend)
    assert set(backend.routes) == {SKILLS_ROUTE}
    assert isinstance(backend.routes[SKILLS_ROUTE], StoreBackend)


def test_routing_actually_reaches_the_git_checkout(checkout: Path) -> None:
    """Structure is not behaviour: read a real file through the composite."""
    backend = build_backend(skill_store=GitSkillStore(checkout))

    listing = backend.ls(SKILLS_ROUTE)
    assert [e["path"] for e in listing.entries] == ["/skills/web-research/"]

    result = backend.read("/skills/web-research/SKILL.md")
    assert result.error is None
    assert "name: web-research" in result.file_data["content"]


def test_store_backend_always_receives_an_explicit_store(checkout: Path) -> None:
    """P1: the harness holds a client it chose, not one the runtime supplies."""
    backend = build_backend(skill_store=GitSkillStore(checkout))
    store_backend = backend.routes[SKILLS_ROUTE]
    assert store_backend._store is not None
    assert isinstance(store_backend._store, GitSkillStore)
    assert store_backend._namespace is not None


def test_the_guard_fires_when_someone_omits_the_store() -> None:
    """§3.4 bans `StoreBackend(store=None)` by name. Make it unreachable."""
    with pytest.raises(StoreBackendWithoutStoreError, match="explicit store="):
        _store_backend(None, lambda _rt: ("skills",))

    with pytest.raises(StoreBackendWithoutStoreError):
        build_backend(skill_store=None)  # type: ignore[arg-type]


def test_only_backends_py_constructs_a_store_backend() -> None:
    """The guard is worth nothing if a second call site can route around it."""
    assert construction_sites("StoreBackend") == ["harness/backends.py"]


def test_context_hub_backend_is_never_constructed() -> None:
    """§3.4: vendor-hosted memory, same ergonomics as `StoreBackend`, opposite risk."""
    assert construction_sites("ContextHubBackend") == []


def test_memories_route_is_a_disabled_hook_not_a_wired_store(checkout: Path) -> None:
    """Phase 3+. Watch item W8: the answer before the gate is "after the number"."""
    backend = build_backend(skill_store=GitSkillStore(checkout))
    assert MEMORIES_ROUTE not in backend.routes

    with pytest.raises(NotImplementedError, match="Phase 3"):
        build_backend(skill_store=GitSkillStore(checkout), memory_store=object())


def test_execution_backend_is_a_constructor_argument(checkout: Path, tmp_path: Path) -> None:
    """Section 4.4: local to cloud is an argument, not a migration."""
    work = tmp_path / "work"
    work.mkdir()
    backend = build_backend(
        skill_store=GitSkillStore(checkout), execution="local_shell", root_dir=work
    )
    assert isinstance(backend.default, LocalShellBackend)
    assert isinstance(backend.routes[SKILLS_ROUTE], StoreBackend)


def test_namespace_is_fixed_rather_than_resolved_from_the_runtime(checkout: Path) -> None:
    backend = build_backend(skill_store=GitSkillStore(checkout), namespace=("pilot", "skills"))
    assert backend.routes[SKILLS_ROUTE]._namespace(None) == ("pilot", "skills")
