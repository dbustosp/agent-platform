"""`GitSkillStore` reads a checkout and refuses to write to it.

§3.3 makes Git the system of record for skills and §3.4 bans skills defined
anywhere else. The read tests prove the harness can see the checkout; the write
tests prove it cannot become the system of record by accident.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from agent_platform.harness.skills_store import GitSkillStore, ReadOnlyStoreError

NS = ("skills",)
SKILL_MD = "---\nname: web-research\ndescription: How to research a topic\n---\n\nDo the thing.\n"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A Git-shaped skills checkout: skill dirs, plus the plumbing to ignore."""
    skill = tmp_path / "web-research"
    skill.mkdir()
    (skill / "SKILL.md").write_text(SKILL_MD)
    (skill / "helper.py").write_text("VALUE = 1\n")

    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


def test_reads_skill_md_from_the_checkout(checkout: Path) -> None:
    store = GitSkillStore(checkout)
    item = store.get(NS, "/web-research/SKILL.md")

    assert item is not None
    assert item.value["content"] == SKILL_MD
    assert item.value["encoding"] == "utf-8"
    assert item.namespace == NS


def test_search_enumerates_every_file_once(checkout: Path) -> None:
    """`StoreBackend.ls` pages through `search` to discover skill directories."""
    keys = [item.key for item in GitSkillStore(checkout).search(NS, limit=100)]
    assert keys == ["/web-research/SKILL.md", "/web-research/helper.py"]


def test_git_plumbing_is_not_served(checkout: Path) -> None:
    """Refs and object files are the store's mechanics, not skill content."""
    store = GitSkillStore(checkout)
    assert all(".git" not in item.key for item in store.search(NS, limit=100))
    assert store.get(NS, "/.git/HEAD") is None


def test_search_paginates(checkout: Path) -> None:
    store = GitSkillStore(checkout)
    assert [i.key for i in store.search(NS, limit=1)] == ["/web-research/SKILL.md"]
    assert [i.key for i in store.search(NS, limit=1, offset=1)] == ["/web-research/helper.py"]


def test_missing_and_escaping_keys_return_nothing(checkout: Path) -> None:
    """Keys arrive from model-generated tool calls, so traversal is a real input."""
    store = GitSkillStore(checkout)
    assert store.get(NS, "/nope/SKILL.md") is None
    assert store.get(NS, "/../../etc/passwd") is None
    assert store.get(NS, "/web-research/../../outside.txt") is None


def test_binary_files_are_base64_rather_than_dropped(checkout: Path) -> None:
    """A skill may ship a diagram; silently dropping it desyncs Git and the agent."""
    payload = b"\x89PNG\r\n\x1a\n\x00\xff"
    (checkout / "web-research" / "diagram.png").write_bytes(payload)

    item = GitSkillStore(checkout).get(NS, "/web-research/diagram.png")
    assert item is not None
    assert item.value["encoding"] == "base64"
    assert base64.standard_b64decode(item.value["content"]) == payload


def test_an_absent_checkout_is_empty_not_an_error(checkout: Path, tmp_path: Path) -> None:
    """A missing checkout means no skills, which the agent can survive."""
    store = GitSkillStore(tmp_path / "never-cloned")
    assert store.search(NS, limit=10) == []
    assert store.get(NS, "/web-research/SKILL.md") is None


def test_writes_raise(checkout: Path) -> None:
    """Skills change by reviewed commit, not by an agent's `write_file`."""
    store = GitSkillStore(checkout)

    with pytest.raises(ReadOnlyStoreError, match="read-only"):
        store.put(NS, "/new-skill/SKILL.md", {"content": "mine now", "encoding": "utf-8"})

    with pytest.raises(ReadOnlyStoreError, match="refusing to write"):
        store.put(NS, "/web-research/SKILL.md", {"content": "edited", "encoding": "utf-8"})


def test_deletes_raise(checkout: Path) -> None:
    with pytest.raises(ReadOnlyStoreError, match="refusing to delete"):
        GitSkillStore(checkout).delete(NS, "/web-research/SKILL.md")


def test_the_checkout_is_untouched_after_a_refused_write(checkout: Path) -> None:
    """Refusing loudly is only half of it; nothing may have been written first."""
    store = GitSkillStore(checkout)
    with pytest.raises(ReadOnlyStoreError):
        store.put(NS, "/new-skill/SKILL.md", {"content": "x", "encoding": "utf-8"})

    assert not (checkout / "new-skill").exists()
    assert (checkout / "web-research" / "SKILL.md").read_text() == SKILL_MD


def test_async_reads_and_refusals_match_the_sync_path(checkout: Path) -> None:
    store = GitSkillStore(checkout)

    async def drive() -> None:
        item = await store.aget(NS, "/web-research/SKILL.md")
        assert item is not None and item.value["content"] == SKILL_MD
        with pytest.raises(ReadOnlyStoreError):
            await store.aput(NS, "/x/SKILL.md", {"content": "x", "encoding": "utf-8"})

    asyncio.run(drive())
