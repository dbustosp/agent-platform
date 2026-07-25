"""Agent identity and run context.

Section 7.4 — "Agent identity as first-class. Non-human identity with lifecycle,
scope, and revocation." Phase 1 does not have that. What Phase 1 has is the
*shape* of it: every evidence row already names the non-human principal that
acted and the human on whose authority it acted, so when real machine identity
lands in Phase 4 it replaces a resolver rather than adding a column.

Deliberately dependency-free — identity is a control-plane concept and must not
import the harness.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from agent_platform.evidence.record import new_run_id


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting, and on whose authority.

    `agent_principal` is the non-human identity. `delegated_by` is the human.
    Both are required: an action with no delegating human is not an action we
    can defend in a review, and in Phase 1 there is always a human at the CLI.
    """

    agent_principal: str
    delegated_by: str

    def __post_init__(self) -> None:
        if not self.agent_principal:
            msg = "agent_principal must be non-empty"
            raise ValueError(msg)
        if not self.delegated_by:
            msg = "delegated_by must be non-empty"
            raise ValueError(msg)

    @classmethod
    def from_env(
        cls,
        *,
        agent_principal: str | None = None,
        delegated_by: str | None = None,
    ) -> Principal:
        """Resolve identity from arguments, then environment, then the OS user.

        Phase 4 replaces the body of this method with a workload-identity
        lookup. Nothing that calls it needs to change.
        """
        agent = agent_principal or os.environ.get("AGENT_PRINCIPAL") or "agent://local/pilot"
        human = (
            delegated_by
            or os.environ.get("DELEGATED_BY")
            or os.environ.get("USER")
            or os.environ.get("USERNAME")
            or "unknown"
        )
        return cls(agent_principal=agent, delegated_by=human)


@dataclass(frozen=True, slots=True)
class RunContext:
    """Everything an evidence row needs that is constant across one task.

    Carried explicitly rather than read from a global, so subagent lineage
    (`parent_run_id`) is a value you pass down instead of state you hope is
    correct.
    """

    principal: Principal
    intent: str
    run_id: str
    parent_run_id: str | None = None
    model_id: str | None = None
    harness_version: str | None = None

    @classmethod
    def start(
        cls,
        intent: str,
        principal: Principal,
        *,
        model_id: str | None = None,
        harness_version: str | None = None,
    ) -> RunContext:
        """Begin a new top-level run."""
        return cls(
            principal=principal,
            intent=intent,
            run_id=new_run_id(),
            parent_run_id=None,
            model_id=model_id,
            harness_version=harness_version,
        )

    def child(self, intent: str | None = None) -> RunContext:
        """Derive a subagent context whose `parent_run_id` points at this run."""
        return replace(
            self,
            run_id=new_run_id(),
            parent_run_id=self.run_id,
            intent=intent if intent is not None else self.intent,
        )
