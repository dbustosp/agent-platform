"""`GitSkillStore` — a read-only store over a Git checkout of `SKILL.md` dirs.

Section 3.3: "Git is the system of record; the harness gets a checkout."
Section 3.4 bans skills defined in framework config, because config is
un-diffable, un-reviewable and un-portable. P4 says the same thing from the
other side: skills are semantic state, so the harness may read them and may not
own them.

That is why every mutating operation here raises. A `write_file` to
`/skills/...` from inside the agent loop would be a skill change with no
review, no diff and no author — the exact failure §3.4 names. Skills change by
commit, and the checkout is refreshed out of band.

**On the base class.** This implements `langgraph.store.base.BaseStore`, not
`langchain_core.stores.BaseStore`. Those are different interfaces
(`batch`/`get`/`search` versus `mget`/`mset`), and `deepagents.backends.
StoreBackend` is typed against and calls the LangGraph one — confirmed by
reading `deepagents/backends/store.py` in the installed wheel. Subclassing the
`langchain_core` one would type-check and then fail at the first `store.get`.

Keys are absolute POSIX paths *relative to the mount point*, e.g.
`/web-research/SKILL.md`. `CompositeBackend` strips the `/skills/` route
prefix before it reaches the store, so the store never sees the mount name.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.store.base import BaseStore as LangGraphBaseStore
from langgraph.store.base import (
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

#: Directories never served to the agent. `.git` is the system of record's
#: plumbing, not content; serving it would put refs and object files into the
#: model's context and into `ls` output.
EXCLUDED_DIRS = frozenset({".git", "__pycache__", ".ruff_cache", ".pytest_cache"})


class ReadOnlyStoreError(RuntimeError):
    """Raised on any attempt to mutate the skills checkout through the harness."""


class GitSkillStore(LangGraphBaseStore):
    """Serve `SKILL.md` directories out of a Git checkout, read-only.

    `namespace` arguments are accepted and echoed back but not used for lookup:
    the checkout *is* the namespace. Scoping happens at the Git layer, where a
    reviewer can see it, rather than in a runtime tuple nobody audits.
    """

    def __init__(self, checkout: str | Path) -> None:
        self.checkout = Path(checkout)

    # -- reads ---------------------------------------------------------------

    def _resolve(self, key: str) -> Path | None:
        """Map a store key to a file inside the checkout, or `None`.

        Returns `None` for anything that escapes the checkout. The key
        originates in a model-generated tool call, so `../../etc/passwd` is a
        realistic input rather than a hypothetical one.
        """
        root = self.checkout.resolve()
        candidate = (root / key.lstrip("/")).resolve()
        if root not in candidate.parents:
            return None
        if EXCLUDED_DIRS.intersection(candidate.relative_to(root).parts):
            return None
        return candidate if candidate.is_file() else None

    def _iter_files(self) -> Iterator[Path]:
        root = self.checkout.resolve()
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if EXCLUDED_DIRS.intersection(path.relative_to(root).parts):
                continue
            yield path

    def _value(self, path: Path) -> dict[str, Any]:
        """Read one file into `StoreBackend`'s v2 file format.

        Binary files are base64-encoded rather than skipped: a skill may ship a
        diagram alongside its `SKILL.md`, and a store that silently drops files
        makes the checkout and the agent's view of it disagree.
        """
        raw = path.read_bytes()
        try:
            return {"content": raw.decode("utf-8"), "encoding": "utf-8"}
        except UnicodeDecodeError:
            return {"content": base64.standard_b64encode(raw).decode("ascii"), "encoding": "base64"}

    def _item(self, namespace: tuple[str, ...], key: str, path: Path) -> Item:
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        value = self._value(path)
        value["modified_at"] = modified.isoformat()
        return Item(
            value=value,
            key=key,
            namespace=namespace,
            created_at=modified,
            updated_at=modified,
        )

    def _get(self, op: GetOp) -> Item | None:
        path = self._resolve(op.key)
        return None if path is None else self._item(op.namespace, op.key, path)

    def _search(self, op: SearchOp) -> list[SearchItem]:
        """Enumerate the checkout. `StoreBackend.ls` pages through this to
        discover skill directories, so every file must appear exactly once."""
        root = self.checkout.resolve()
        items: list[SearchItem] = []
        for path in self._iter_files():
            item = self._item(op.namespace_prefix, "/" + path.relative_to(root).as_posix(), path)
            items.append(
                SearchItem(
                    namespace=item.namespace,
                    key=item.key,
                    value=item.value,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )
        return items[op.offset : op.offset + op.limit]

    # -- writes: refused ------------------------------------------------------

    def _refuse(self, op: PutOp) -> None:
        verb = "delete" if op.value is None else "write"
        msg = (
            f"GitSkillStore is read-only: refusing to {verb} {op.key!r}. "
            "Skills are system-of-record state owned by Git (plan §3.3/§3.4); "
            "change them by commit, then refresh the checkout."
        )
        raise ReadOnlyStoreError(msg)

    # -- BaseStore surface ----------------------------------------------------

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """Dispatch the four `BaseStore` operations. `batch` is the only hook.

        Everything else on `BaseStore` (`get`, `search`, `put`, `delete`, and
        the async variants) is a concrete method that funnels through here, so
        refusing `PutOp` in one place closes every write path at once.
        """
        results: list[Result] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self._get(op))
            elif isinstance(op, SearchOp):
                results.append(self._search(op))
            elif isinstance(op, PutOp):
                self._refuse(op)
            elif isinstance(op, ListNamespacesOp):
                # One checkout, one namespace; nothing to enumerate.
                results.append([])
            else:  # pragma: no cover - defensive against a new Op type
                msg = f"GitSkillStore does not support {type(op).__name__}"
                raise NotImplementedError(msg)
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """Async surface over the same synchronous reads.

        Reading a handful of local files is not worth an async filesystem
        dependency, and the checkout is on local disk by construction — §3.4
        forbids a hosted service in the runtime critical path.
        """
        return self.batch(ops)
