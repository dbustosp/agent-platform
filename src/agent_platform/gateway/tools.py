"""The Section 3.3 tool surface, and the choke point every call goes through.

These are plain Python callables. No MCP types, no LangChain types, no
framework objects in any signature — `server.py` wraps them for MCP and the
harness may call them directly, and neither of those is allowed to become the
only way to reach them (P1).

**Where the control lives.** `Gateway.call` evaluates policy *before* it looks
up a handler, and the handlers are private. There is no public path to a tool
body that skips the check. That is P2's test answered directly: *can a control
be bypassed by changing the system prompt or adding custom middleware?* Delete
every middleware in the harness and the answer here does not move, because the
decision is not taken there.

**Identity comes from the connection, never from the arguments.** The principal
that policy is evaluated against is the one bound to this `Gateway` instance.
A caller can put `principal`, `policy_decision` or anything else in the argument
dict; all it changes is the `args_hash` on the refusal row.

**One call, one evidence row — including refusals.** A denied call is precisely
what an audit asks about ("did it ever try?"), so the row is written before
`PolicyDenied` is raised. Escalations return a structured approval request
rather than raising, and also write a row. The single exception to "the gateway
authors the row" is `record_evidence`, whose row *is* the record it was handed;
authoring a second one would double-count one action.

Evidence is emitted here because this is the point of action for these tools —
the gateway holds the authoritative policy decision, and P3 says the record is
the original, not a copy reconstructed from something that watched it happen.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from agent_platform.evidence.hashing import EMPTY, hash_args, hash_result
from agent_platform.evidence.record import EvidenceRecord, PolicyDecision
from agent_platform.evidence.sink import EvidenceSink
from agent_platform.gateway.knowledge import KnowledgeSource
from agent_platform.identity import RunContext
from agent_platform.policy import (
    AllowAllPolicy,
    AllowlistPolicy,
    PolicyDenied,
    PolicyEngine,
    PolicyRequest,
    PolicyResult,
)

logger = logging.getLogger(__name__)

#: The three read tools Phase 1 exposes to agents (§5, "read-only, 2-4 tools").
READ_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "lookup_service_context",
        "get_control_requirements",
        "recall",
    }
)

#: Write-class tools. Not on the agent-facing MCP surface — see `server.py`.
WRITE_TOOL_NAMES: frozenset[str] = frozenset({"record_evidence"})

#: Every tool whose evidence row the gateway writes itself.
#:
#: The harness middleware reads this to decide what *not* to log: a gateway tool
#: already has a row carrying the authoritative policy decision, and a second
#: row from the middleware would be a copy of a record we already hold (P3).
GATEWAY_TOOL_NAMES: frozenset[str] = READ_TOOL_NAMES | WRITE_TOOL_NAMES

#: Tools that emit their own evidence, so dispatch must not emit a second row.
_SELF_LOGGING_TOOLS: frozenset[str] = frozenset({"record_evidence"})


class UnknownGatewayTool(LookupError):
    """Policy permitted a tool name the gateway does not implement.

    A configuration error, not an agent error: something allowlisted a name that
    no handler serves. It still produces an evidence row, because an allow
    decision was taken and we record decisions, not just outcomes.
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"gateway has no tool named {tool!r}")


def read_only_policy() -> AllowlistPolicy:
    """Default-deny allowlist permitting exactly the three read tools.

    This is the policy for an agent's gateway connection in Phase 1. Note what
    is absent: `record_evidence`. A harness-side gateway that needs it must
    allowlist it explicitly, in code someone reviewed.
    """
    return AllowlistPolicy(allowed_tools=frozenset(READ_TOOL_NAMES))


class Gateway:
    """A knowledge source, a policy engine and an evidence sink, wired together.

    All three are constructor arguments with no defaults. The gateway will not
    invent a sink for itself: "evidence gap discovered late" is the one risk the
    register rates Severe, and a component that silently starts without
    somewhere to put evidence is how that happens.
    """

    def __init__(
        self,
        knowledge: KnowledgeSource,
        policy: PolicyEngine,
        sink: EvidenceSink,
        context: RunContext,
    ) -> None:
        self._knowledge = knowledge
        self._policy = policy
        self._sink = sink
        self._context = context
        self._handlers: dict[str, Callable[..., Any]] = {
            "lookup_service_context": self._handle_lookup_service_context,
            "get_control_requirements": self._handle_get_control_requirements,
            "recall": self._handle_recall,
            "record_evidence": self._handle_record_evidence,
        }
        if isinstance(policy, AllowAllPolicy):
            # `AllowAllPolicy` documents that the gateway warns when it is used.
            # It is a development escape hatch, and a gateway wired to one is a
            # gateway with no control at the tool boundary (P2) — that must be
            # visible in the operator's log, not only in a docstring.
            logger.warning(
                "gateway for principal %s is running with AllowAllPolicy: every tool call "
                "will be permitted. Development only — never wire this to a real system.",
                context.principal.agent_principal,
            )

    @property
    def context(self) -> RunContext:
        """The run this gateway is acting for. Read-only by design."""
        return self._context

    def with_context(self, context: RunContext) -> Gateway:
        """A gateway bound to a different run, sharing knowledge, policy, sink.

        This is the only way to change the acting identity, and it is a
        construction-time act by the host process. Subagent lineage flows
        through `RunContext.child()`, so the caller cannot reach a new principal
        through a tool argument.
        """
        return Gateway(self._knowledge, self._policy, self._sink, context)

    # -- Tool surface (Section 3.3) --------------------------------------

    def lookup_service_context(self, service: str) -> dict[str, Any]:
        """Ownership, dependencies and criticality for a service or repo."""
        return self.call("lookup_service_context", {"service": service})

    def get_control_requirements(self, change_class: str) -> dict[str, Any]:
        """Applicable controls and standards for a class of change."""
        return self.call("get_control_requirements", {"change_class": change_class})

    def recall(self, subject: str) -> dict[str, Any]:
        """Prior assertions relevant to a subject."""
        return self.call("recall", {"subject": subject})

    def record_evidence(self, record: EvidenceRecord | Mapping[str, Any]) -> dict[str, Any]:
        """Write an evidence record. Called by the harness middleware (§3.3)."""
        return self.call("record_evidence", {"record": record})

    # -- Dispatch: the choke point ---------------------------------------

    def call(self, tool: str, args: Mapping[str, Any] | None = None) -> Any:
        """Authorise, execute, record. In that order, with no way round it.

        The order is load-bearing. Policy runs before the handler lookup so that
        a probe for a tool that does not exist is refused and logged rather than
        raising a shape that tells the caller which names are real. On a denial
        the row is written and then `PolicyDenied` is raised — if a fail-closed
        sink turns that into an `EvidenceSinkError` the call still fails, which
        is the safe direction.
        """
        call_args = dict(args or {})
        result = self._policy.evaluate(
            PolicyRequest(tool=tool, args=call_args, principal=self._context.principal)
        )

        if result.decision is PolicyDecision.DENY:
            self._emit(tool, call_args, result, EMPTY)
            raise PolicyDenied(result, tool)

        if result.decision is PolicyDecision.ESCALATE:
            self._emit(tool, call_args, result, EMPTY)
            return {
                "status": "approval_required",
                "tool": tool,
                "reason": result.reason,
                "approval_ref": result.approval_ref,
            }

        handler = self._handlers.get(tool)
        if handler is None:
            error = UnknownGatewayTool(tool)
            self._emit(tool, call_args, result, hash_result(_error_marker(error)))
            raise error

        try:
            payload = handler(**call_args)
        except Exception as exc:
            # A tool that blew up is still something the agent did. Record it,
            # then let it propagate — swallowing it would hide the failure from
            # the caller while leaving a row that claims nothing went wrong.
            self._emit(tool, call_args, result, hash_result(_error_marker(exc)))
            raise

        if tool not in _SELF_LOGGING_TOOLS:
            self._emit(tool, call_args, result, hash_result(payload))
        return payload

    # -- Handlers (private: reachable only through `call`) ----------------

    def _handle_lookup_service_context(self, service: str) -> dict[str, Any]:
        context = self._knowledge.service_context(service)
        if context is None:
            return {
                "service": service,
                "found": False,
                "detail": f"no service context recorded for {service!r}",
            }
        return {"found": True, **context.to_dict()}

    def _handle_get_control_requirements(self, change_class: str) -> dict[str, Any]:
        requirements = self._knowledge.control_requirements(change_class)
        if requirements is None:
            return {
                "change_class": change_class,
                "found": False,
                "detail": f"no control requirements recorded for change class {change_class!r}",
            }
        return {"found": True, **requirements.to_dict()}

    def _handle_recall(self, subject: str) -> dict[str, Any]:
        assertions = self._knowledge.assertions(subject)
        return {
            "subject": subject,
            "found": bool(assertions),
            "count": len(assertions),
            "assertions": [a.to_dict() for a in assertions],
        }

    def _handle_record_evidence(self, record: EvidenceRecord | Mapping[str, Any]) -> dict[str, Any]:
        """Put a caller's record into our store, unchanged.

        The gateway does not re-author the row. The caller — in Phase 1, the
        harness middleware — was at the point of action for the tool call being
        described, and it holds the model id, the harness version and the
        outcome. Rewriting any of that here would replace a first-hand record
        with a second-hand one, which is exactly what P3 forbids.
        """
        evidence = (
            record if isinstance(record, EvidenceRecord) else EvidenceRecord.from_dict(dict(record))
        )
        self._sink.emit(evidence)
        return {
            "recorded": True,
            "run_id": evidence.run_id,
            "tool": evidence.tool,
            "timestamp": evidence.timestamp.isoformat(),
        }

    # -- Evidence ---------------------------------------------------------

    def _emit(
        self,
        tool: str,
        args: Mapping[str, Any],
        result: PolicyResult,
        result_hash: str,
    ) -> EvidenceRecord:
        """Author one row for this call from the gateway's own run context."""
        record = EvidenceRecord(
            run_id=self._context.run_id,
            parent_run_id=self._context.parent_run_id,
            agent_principal=self._context.principal.agent_principal,
            delegated_by=self._context.principal.delegated_by,
            intent=self._context.intent,
            tool=tool,
            args_hash=hash_args(args),
            policy_decision=result.decision,
            approval_ref=result.approval_ref,
            result_hash=result_hash,
            model_id=self._context.model_id,
            harness_version=self._context.harness_version,
        )
        self._sink.emit(record)
        return record


def _error_marker(exc: BaseException) -> dict[str, str]:
    """What we hash when a call produced no result.

    Hashed, not stored — §6 keeps payloads out of the log, and an exception
    message is a payload like any other.
    """
    return {"error": type(exc).__name__, "message": str(exc)}
