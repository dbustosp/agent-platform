"""`EvidenceMiddleware` — one evidence row per tool call, at the point of action.

Sections 4.2, 4.3 and 6. This is the only middleware Phase 1 genuinely
requires; everything else in the stack is convenience.

P3 — *evidence is emitted at the point of action, not reconstructed from
traces.* `wrap_tool_call` fires once per tool call, wrapped around execution,
which is the closest hook the harness offers to the action itself. The row is
written by us, to our sink, before the result reaches the model. If the tracing
vendor were removed tomorrow the audit trail would be untouched.

Five behaviours are load-bearing and are covered by tests:

1. A tool that raises still produces a row. The failure is the interesting
   half of an audit trail, and the exception still propagates — this middleware
   observes, it does not handle.
2. A LangGraph control signal is *not* an outcome. `GraphBubbleUp` (interrupts,
   parent commands) means the tool has not finished, so it is re-raised
   unrecorded; the resumed call writes the real row.
3. A sink failure never breaks the agent loop. `SpillingSink` exists so the row
   survives; this `except` exists so the run survives even when that fails.
4. Gateway-owned tools are skipped. The gateway logs its own calls with the
   real policy decision (P2); logging them here as well would double-count
   every row and attribute an `allow` we did not make.
5. `result_hash` digests the payload, never the envelope — for both halves of
   `wrap_tool_call`'s `ToolMessage | Command` return type. Message ids are
   regenerated on every call; a digest that includes them answers nothing.

Section 4.3 caps custom middleware at 1,500 LOC because it is non-portable by
construction, and asks for the number to be tracked. Measure it rather than
trusting this sentence — a hand-typed figure goes stale on the next edit:

    wc -l src/agent_platform/harness/middleware.py    # 224, 2026-07-25

This module plus `agent._tool_error_to_model` is the whole of the platform's
custom-middleware spend so far, comfortably inside the ceiling. Anything that
wants to grow it materially belongs in an MCP server, where it is portable —
that is what the ceiling is for (W2).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import deepagents
from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import BaseMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command

from agent_platform.evidence.hashing import hash_args, hash_result
from agent_platform.evidence.record import EvidenceRecord, PolicyDecision
from agent_platform.evidence.sink import EvidenceSink
from agent_platform.identity import RunContext

logger = logging.getLogger(__name__)

#: Section 6: `harness_version` is a *field, not a dependency*. Recorded so a
#: row read in three years names the thing that produced it, without anyone
#: needing that thing to still exist.
HARNESS_VERSION = f"deepagents=={deepagents.__version__}"


def _gateway_tool_names() -> frozenset[str]:
    """Resolve gateway-owned tool names without depending on the gateway.

    Imported inside the function on purpose: `harness` must not require
    `gateway` to be installed or importable (P5 — every layer independently
    deployable and independently killable). Absent gateway, nothing is skipped.
    """
    try:
        from agent_platform.gateway import GATEWAY_TOOL_NAMES
    except ImportError:
        return frozenset()
    return frozenset(GATEWAY_TOOL_NAMES)


def _message_payload(message: BaseMessage) -> dict[str, Any]:
    """The parts of a message that are the payload rather than the envelope."""
    return {"content": message.content, "status": getattr(message, "status", None)}


def _command_payload(command: Command) -> dict[str, Any]:
    """Strip message ids out of a `Command`'s state update.

    `Command.update` usually carries `messages`, and every message in it is
    built fresh — with a fresh random `id` — on each call. Digesting the
    `Command` as-is therefore falls through `hashing._canonicalise` to `repr()`
    and embeds those ids, so two identical calls hash differently.
    """
    update = command.update
    if isinstance(update, dict):
        canonical: dict[str, Any] = {}
        for key, value in update.items():
            if isinstance(value, list):
                canonical[key] = [
                    _message_payload(v) if isinstance(v, BaseMessage) else v for v in value
                ]
            elif isinstance(value, BaseMessage):
                canonical[key] = _message_payload(value)
            else:
                canonical[key] = value
    else:
        canonical = {"__update__": update}
    return {"__command__": True, "goto": command.goto, "graph": command.graph, **canonical}


def _result_digest(result: Any) -> str:
    """Hash the tool's payload, not the envelope it arrived in.

    A `ToolMessage` carries a fresh random `id` on every call, so hashing the
    whole object would make two identical tool calls produce different digests
    and destroy the only question `result_hash` exists to answer.

    `wrap_tool_call` returns `ToolMessage | Command` (VERIFIED-API.md), and the
    `Command` half needs the same treatment for the same reason — deepagents'
    `write_todos` and the `task` subagent tool both return one.
    """
    if isinstance(result, Command):
        return hash_result(_command_payload(result))
    if isinstance(result, BaseMessage):
        return hash_result(_message_payload(result))
    content = getattr(result, "content", None)
    if content is None:
        return hash_result(result)
    return hash_result({"content": content, "status": getattr(result, "status", None)})


def _failure_digest(exc: BaseException) -> str:
    """Hash a raised exception so a failed call still has a `result_hash`."""
    return hash_result({"__error__": type(exc).__name__, "message": str(exc)})


class EvidenceMiddleware(AgentMiddleware):
    """Emit exactly one `EvidenceRecord` per tool call.

    `policy_decision` is a constructor argument, not a computation. P2 puts the
    decision at the tool boundary inside the gateway; middleware may *observe*
    it in order to record it but may not take it. What this middleware knows is
    that the call was dispatched, which for harness-local tools means the
    boundary allowed it.
    """

    def __init__(
        self,
        sink: EvidenceSink,
        run_context: RunContext,
        *,
        skip_tools: Iterable[str] | None = None,
        policy_decision: PolicyDecision = PolicyDecision.ALLOW,
    ) -> None:
        super().__init__()
        self.tools = []
        self.sink = sink
        self.run_context = run_context
        self.skip_tools = frozenset(skip_tools) if skip_tools is not None else _gateway_tool_names()
        self.policy_decision = policy_decision

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        name = request.tool_call.get("name") or "<unknown>"
        if name in self.skip_tools:
            return handler(request)
        args = request.tool_call.get("args")
        try:
            result = handler(request)
        except GraphBubbleUp:
            # Not an outcome. An interrupt or a parent command means the tool
            # has not finished; the call is replayed on resume and records its
            # real result then. Recording here would claim a failure that did
            # not happen and double-count the call. `ToolErrorMiddleware`
            # guards the same signal for the same reason.
            raise
        except Exception as exc:
            self._record(name, args, _failure_digest(exc))
            raise
        self._record(name, args, _result_digest(result))
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        name = request.tool_call.get("name") or "<unknown>"
        if name in self.skip_tools:
            return await handler(request)
        args = request.tool_call.get("args")
        try:
            result = await handler(request)
        except GraphBubbleUp:
            raise  # See `wrap_tool_call`.
        except Exception as exc:
            self._record(name, args, _failure_digest(exc))
            raise
        self._record(name, args, _result_digest(result))
        return result

    def _record(self, tool: str, args: Any, result_hash: str) -> None:
        ctx = self.run_context
        record = EvidenceRecord(
            run_id=ctx.run_id,
            parent_run_id=ctx.parent_run_id,
            agent_principal=ctx.principal.agent_principal,
            delegated_by=ctx.principal.delegated_by,
            intent=ctx.intent,
            tool=tool,
            args_hash=hash_args(args),
            policy_decision=self.policy_decision,
            result_hash=result_hash,
            model_id=ctx.model_id,
            harness_version=ctx.harness_version or HARNESS_VERSION,
        )
        try:
            self.sink.emit(record)
        except Exception:
            # An evidence gap is severe (Section 9) but a dead agent produces
            # no evidence at all. Log loudly, keep running.
            logger.exception("evidence sink failed for tool %r; agent continues", tool)
