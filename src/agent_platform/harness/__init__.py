"""The Deep Agents harness — the replaceable half of the platform.

Section 2.1 rents this layer and buys nothing from it. Expected useful life is
12-24 months (Section 7.1), so the code here is written to be *cheap to delete*
rather than cheap to extend: every module is small, every dependency on
`deepagents` is local to this package, and nothing durable is stored by it.

This is the only package permitted to import `deepagents` or `langchain`
(see `agent_platform/__init__.py`). Importing it pulls the framework in; the
control plane never does, which is P1 made mechanical.

What lives here and why:

- `testing`      — `ScriptedChatModel`, so the suite runs with no API key.
- `middleware`   — `EvidenceMiddleware`, the one non-negotiable piece (P3).
- `skills_store` — read-only view of the Git skills checkout (P1, P4).
- `backends`     — Section 4.1 routing, with the `StoreBackend(store=...)` guard.
- `models`       — explicit model construction; `model=None` is deprecated.
- `agent`        — Section 4.2 assembly, which refuses to build without evidence.
"""

from agent_platform.harness.agent import (
    MissingEvidenceMiddleware,
    build_agent,
    default_permissions,
    load_prompt,
)
from agent_platform.harness.backends import (
    MEMORIES_ROUTE,
    SKILLS_ROUTE,
    StoreBackendWithoutStoreError,
    build_backend,
)
from agent_platform.harness.middleware import HARNESS_VERSION, EvidenceMiddleware
from agent_platform.harness.models import UnavailableModelProvider, build_model
from agent_platform.harness.skills_store import GitSkillStore, ReadOnlyStoreError
from agent_platform.harness.testing import ScriptedChatModel, tool_turn

__all__ = [
    "HARNESS_VERSION",
    "MEMORIES_ROUTE",
    "SKILLS_ROUTE",
    "EvidenceMiddleware",
    "GitSkillStore",
    "MissingEvidenceMiddleware",
    "ReadOnlyStoreError",
    "ScriptedChatModel",
    "StoreBackendWithoutStoreError",
    "UnavailableModelProvider",
    "build_agent",
    "build_backend",
    "build_model",
    "default_permissions",
    "load_prompt",
    "tool_turn",
]
