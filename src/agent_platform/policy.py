"""Policy decisions at the tool boundary.

P2 — enforce at the tool boundary, never in the prompt or the loop.
*Test: can a control be bypassed by changing the system prompt or adding custom
middleware? If yes, it is not a control.*

That test dictates where this module may be called from. The policy check runs
inside the gateway's tool dispatch, before the tool body executes. Middleware
may *observe* the decision in order to record it, but the decision is not taken
there and cannot be reversed there — removing every middleware in the harness
changes what gets logged, not what gets allowed.

Dependency-free by construction. A policy engine that imports the harness is a
policy engine that ships with the harness.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent_platform.evidence.record import PolicyDecision
from agent_platform.identity import Principal


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    """A single authorisation question."""

    tool: str
    args: Mapping[str, Any]
    principal: Principal


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """The answer, plus why — the reason lands in logs, not in the evidence row.

    Section 6 records `policy_decision` and `approval_ref` only. The reason
    string is operator ergonomics; keeping it out of the record avoids leaking
    argument values into a store that is deliberately payload-free.
    """

    decision: PolicyDecision
    reason: str = ""
    approval_ref: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW


@runtime_checkable
class PolicyEngine(Protocol):
    """Decides allow / deny / escalate for one tool call."""

    def evaluate(self, request: PolicyRequest) -> PolicyResult: ...


class PolicyDenied(PermissionError):
    """Raised when a tool call is refused at the boundary."""

    def __init__(self, result: PolicyResult, tool: str) -> None:
        self.result = result
        self.tool = tool
        super().__init__(f"policy denied tool {tool!r}: {result.reason or result.decision.value}")


@dataclass
class AllowlistPolicy:
    """Default-deny allowlist with an explicit write-class escalation set.

    Phase 1 exposes read-only tools, so in practice everything either allows or
    denies. `escalate` exists now rather than later because Phase 4 adds HITL
    approval on write-class actions, and a decision enum that only ever emits
    two of its three values is one nobody trusts when the third finally appears.
    """

    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    #: Tools that require human approval before they execute.
    escalate_tools: frozenset[str] = field(default_factory=frozenset)
    #: Principals permitted to use the gateway at all. Empty means "any".
    allowed_principals: frozenset[str] = field(default_factory=frozenset)

    def evaluate(self, request: PolicyRequest) -> PolicyResult:
        principal = request.principal.agent_principal
        if self.allowed_principals and principal not in self.allowed_principals:
            return PolicyResult(
                PolicyDecision.DENY,
                f"principal {request.principal.agent_principal!r} is not on the gateway allowlist",
            )
        if request.tool in self.escalate_tools:
            return PolicyResult(
                PolicyDecision.ESCALATE,
                f"tool {request.tool!r} is write-class and requires approval",
            )
        if request.tool in self.allowed_tools:
            return PolicyResult(PolicyDecision.ALLOW, "allowlisted")
        return PolicyResult(
            PolicyDecision.DENY,
            f"tool {request.tool!r} is not on the allowlist (default deny)",
        )


class AllowAllPolicy:
    """Permits everything. Test fixture and local-dev escape hatch only.

    Never wire this into anything that reaches a real internal system; the
    gateway logs a warning when it is used.
    """

    def evaluate(self, request: PolicyRequest) -> PolicyResult:
        return PolicyResult(PolicyDecision.ALLOW, "allow-all policy (development only)")
