"""Backend routing — Section 4.1, and the one keyword argument that matters.

```python
CompositeBackend(
    default=StateBackend(),                                  # ephemeral: fine
    routes={"/skills/": StoreBackend(store=..., namespace=...)},
)
```

`StoreBackend.__init__` defaults `store=None`, and when it is `None` the store
is resolved from the LangGraph runtime at call time via `get_store()`. That is
the coupling P1 forbids: the harness would be handed a store instead of holding
a client to one we chose. §3.4 lists `StoreBackend(store=None)` by name because
it fails silently and looks identical to the correct call.

A convention would not survive a hurried afternoon, so this module makes it
structural: `_store_backend()` is the only place in the package that names
`StoreBackend`, and it raises before construction if `store` is falsy.
`tests/test_backends.py` asserts both the guard and — by source inspection —
that nothing anywhere in `agent_platform` constructs `ContextHubBackend`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from deepagents.backends import (
    BackendProtocol,
    CompositeBackend,
    LocalShellBackend,
    StateBackend,
    StoreBackend,
)
from langgraph.store.base import BaseStore

#: Mount point for the Git skills checkout. `create_deep_agent(skills=[...])`
#: is pointed at the same string.
SKILLS_ROUTE = "/skills/"

#: Phase 3+ — deliberately not wired. Section 3.3: "Do not build a memory
#: platform before there is memory worth keeping", and watch item W8 says the
#: answer to a memory request before the gate is "after the number". The
#: constant exists so the route name is decided; `build_backend` raises if a
#: caller tries to use it, so the deferral is a failure rather than a drift.
MEMORIES_ROUTE = "/memories/"

#: Section 4.4: local versus cloud execution is a constructor argument, not a
#: migration. Phase 1 is `"local_shell"`; `"state"` is the credential-free,
#: side-effect-free default the test suite runs on.
ExecutionMode = Literal["state", "local_shell"]

NamespaceFactory = Callable[[Any], tuple[str, ...]]


class StoreBackendWithoutStoreError(ValueError):
    """Raised when something tries to build a `StoreBackend` with no store."""


def _store_backend(store: BaseStore | None, namespace: NamespaceFactory) -> StoreBackend:
    """Construct the only `StoreBackend` this package is allowed to construct.

    Keeping the class name in exactly one function is what makes the ban
    enforceable by reading: a grep for `StoreBackend(` that returns anything
    outside this file is a review finding.
    """
    if store is None:
        msg = (
            "StoreBackend requires an explicit store= argument. Passing None "
            "resolves the store from the LangGraph runtime at call time, which "
            "is the framework coupling P1 forbids and §3.4 bans by name."
        )
        raise StoreBackendWithoutStoreError(msg)
    return StoreBackend(store=store, namespace=namespace)


def build_backend(
    *,
    skill_store: BaseStore,
    namespace: tuple[str, ...] = ("skills",),
    execution: ExecutionMode = "state",
    root_dir: str | Path | None = None,
    memory_store: object = None,
) -> CompositeBackend:
    """Assemble the Section 4.1 backend.

    Args:
        skill_store: The skills checkout. Required — see `_store_backend`.
        namespace: Store namespace for the skills route. Fixed rather than
            derived from the runtime, because the checkout is the scope.
        execution: `"state"` keeps scratch files in graph state (ephemeral,
            harness-owned, explicitly permitted by §3.4). `"local_shell"` is
            the Phase 1 execution backend from §4.4.
        root_dir: Working directory for `"local_shell"`.
        memory_store: Reserved. Passing anything raises — see `MEMORIES_ROUTE`.

    The default backend owns only what §3.4 says the harness may own:
    conversation state and scratch files. Everything durable is behind a route.
    """
    if memory_store is not None:
        msg = (
            "A memory store is Phase 3+ and is deliberately not wired "
            "(plan §3.3, watch item W8). Do not route /memories/ here without "
            "the gateway-backed store the plan calls for."
        )
        raise NotImplementedError(msg)

    default: BackendProtocol
    if execution == "local_shell":
        # `virtual_mode=True` is required, not cosmetic: it gives the virtual
        # path semantics `CompositeBackend` routing assumes, and it stops
        # absolute paths and `..` from walking out of `root_dir`. It is a
        # guardrail and not a sandbox — sandboxes are Phase 3 (§4.4), and
        # `execute()` still runs on the host.
        default = LocalShellBackend(root_dir=root_dir, virtual_mode=True)
    else:
        default = StateBackend()

    return CompositeBackend(
        default=default,
        routes={SKILLS_ROUTE: _store_backend(skill_store, lambda _rt: namespace)},
    )
