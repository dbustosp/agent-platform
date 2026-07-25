"""Agent assembly — Section 4.2, with the non-negotiable made structural.

The plan calls `EvidenceMiddleware` non-negotiable and Section 9 rates a late
evidence gap as the one Severe risk on the register: it is the single piece
that cannot be backfilled. "Non-negotiable" written in a document is a
convention, and conventions lose to schedule pressure. So `build_agent` refuses
to return an agent whose middleware stack cannot produce evidence — you can
still build a Deep Agent by calling `create_deep_agent` yourself, but you
cannot do it through this function and pretend it is the platform's agent.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from langchain.agents.middleware import (
    AgentMiddleware,
    InterruptOnConfig,
    ModelCallLimitMiddleware,
    ToolErrorMiddleware,
)
from langchain_core.language_models import BaseChatModel

from agent_platform.evidence.sink import EvidenceSink
from agent_platform.harness.backends import MEMORIES_ROUTE, SKILLS_ROUTE
from agent_platform.harness.middleware import EvidenceMiddleware
from agent_platform.identity import RunContext

#: Default `ModelCallLimitMiddleware(run_limit=...)`. A bounded loop is a
#: precondition for a pilot on an engineer's machine; `exit_behavior="end"`
#: makes hitting it a finished run with evidence rather than a crash.
DEFAULT_RUN_LIMIT = 25

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"


class MissingEvidenceMiddleware(ValueError):
    """Raised when a caller supplies a middleware stack with no evidence."""


def _tool_error_to_model(exc: Exception, request: Any) -> str:
    """Turn a tool exception into something the model can act on.

    §4.2 writes `ToolErrorMiddleware()` bare, but langchain 1.3.14 refuses to
    construct without a handler — error handling is opt-in so internal
    exceptions are never serialised to the model by accident. This handler
    names the exception *type* and not its message, on the same reasoning that
    keeps payloads out of the evidence log: the type is enough to decide what
    to do next, the message may carry internal detail.
    """
    return (
        f"Tool {request.tool_call['name']!r} failed with {type(exc).__name__}. "
        "Report the failure to the user. Do not retry blindly and do not "
        "invent the result the tool would have returned."
    )


def load_prompt(path: str | Path | None = None) -> str:
    """Read the system prompt from a file.

    A file, not a string literal: the prompt is reviewable and diffable for the
    same reason skills are (§3.4). It is also not a control — P2 is explicit
    that anything a prompt edit can bypass was never a control — so nothing in
    this repository depends on its wording for enforcement.
    """
    return Path(path or _PROMPT_PATH).read_text(encoding="utf-8")


def default_permissions() -> list[FilesystemPermission]:
    """Restrictive filesystem defaults. First matching rule wins.

    `deepagents` falls through to `allow`, so the last rule is a catch-all deny
    on writes: the harness may own scratch space and nothing else. Rules follow
    the §3.4 portability contract directly —

    - skills are Git's, so they are readable and never writable;
    - `/memories/` is Phase 3+ and is denied outright rather than quietly
      landing in whatever backend happens to serve the path;
    - `/tmp/` and `/workspace/` are the two locations §3.4 names as
      harness-owned scratch, and are the only writable ones.

    Reads outside these paths are left alone: `LocalShellBackend` needs to read
    the checkout it was pointed at, and a read-side catch-all would make the
    Phase 1 use case impossible while adding no control the gateway does not
    already provide at the tool boundary.
    """
    skills_glob = f"{SKILLS_ROUTE}**"
    memories_glob = f"{MEMORIES_ROUTE}**"
    return [
        FilesystemPermission(operations=["write"], paths=[skills_glob], mode="deny"),
        FilesystemPermission(operations=["read", "write"], paths=[memories_glob], mode="deny"),
        FilesystemPermission(operations=["read"], paths=[skills_glob], mode="allow"),
        FilesystemPermission(
            operations=["read", "write"], paths=["/tmp/**", "/workspace/**"], mode="allow"
        ),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]


def _evidence_middleware(middleware: Sequence[AgentMiddleware]) -> EvidenceMiddleware | None:
    return next((m for m in middleware if isinstance(m, EvidenceMiddleware)), None)


def _audited_subagents(
    middleware: Sequence[AgentMiddleware], skills: Sequence[str]
) -> list[dict[str, Any]]:
    """Give the general-purpose subagent an `EvidenceMiddleware` of its own.

    `create_deep_agent` adds a general-purpose subagent — and the `task` tool
    that reaches it — unless the caller supplies a spec of that name. It builds
    that subagent's middleware stack itself and does *not* copy ours into it,
    so every tool call made inside a delegated task lands with no evidence row.
    Verified against `deepagents` 0.6.12: the subagent's tool body runs and the
    only row written is the parent's `task` call.

    That is an evidence gap of exactly the kind Section 9 rates Severe, and
    Section 6 already has the field for the fix: `parent_run_id`, "subagent
    lineage". Supplying the spec ourselves is the supported way to override the
    default, and it is the only place `RunContext.child()` has a caller.

    The child context is created once per agent, so repeated delegations within
    one task share a subagent `run_id`. Rows still name the parent run and are
    still distinguishable from the main agent's. Per-delegation run ids need a
    per-invocation hook the harness does not expose in 0.6.12.
    """
    evidence = _evidence_middleware(middleware)
    if evidence is None:  # pragma: no cover - `_require_evidence` runs first
        return []
    child = EvidenceMiddleware(
        evidence.sink,
        evidence.run_context.child(),
        skip_tools=evidence.skip_tools,
        policy_decision=evidence.policy_decision,
    )
    # `tools`, `permissions` and `interrupt_on` are deliberately absent: absent
    # from the spec, `create_deep_agent` gives the subagent the parent's.
    return [{**GENERAL_PURPOSE_SUBAGENT, "middleware": [child], "skills": list(skills)}]


def _require_evidence(middleware: Sequence[AgentMiddleware]) -> None:
    if _evidence_middleware(middleware) is None:
        supplied = ", ".join(type(m).__name__ for m in middleware) or "(none)"
        msg = (
            "build_agent requires an EvidenceMiddleware in the middleware stack; "
            f"got: {supplied}. Section 4.2 calls it non-negotiable and Section 9 "
            "rates a late evidence gap as the only Severe risk on the register — "
            "it is the one thing that cannot be backfilled. Pass sink= and "
            "run_context= to get the default stack, or include one yourself."
        )
        raise MissingEvidenceMiddleware(msg)


def build_agent(
    *,
    model: BaseChatModel,
    backend: Any,
    tools: Sequence[Any] = (),
    sink: EvidenceSink | None = None,
    run_context: RunContext | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    system_prompt: str | None = None,
    skills: Sequence[str] = (SKILLS_ROUTE,),
    permissions: list[FilesystemPermission] | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    run_limit: int = DEFAULT_RUN_LIMIT,
    name: str | None = None,
) -> Any:
    """Build the Phase 1 agent exactly as Section 4.2 specifies it.

    Args:
        model: Explicit chat model. `model=None` is deprecated upstream and is
            not accepted here — see `models.build_model`.
        backend: Usually `backends.build_backend(...)`.
        tools: Gateway MCP tools. Phase 1 is read-only, 2-4 of them.
        sink, run_context: Required unless `middleware` is supplied; they build
            the default `EvidenceMiddleware`.
        middleware: Full replacement stack. Must contain an `EvidenceMiddleware`.
        interrupt_on: HITL on write-class tools. `None` in Phase 1 because the
            gateway is read-only; the hook is here so Phase 4 is configuration.

    Returns:
        A compiled LangGraph agent.

    Raises:
        MissingEvidenceMiddleware: if `middleware` cannot produce evidence.
    """
    if middleware is not None and (sink is not None or run_context is not None):
        # Supplying both is always a mistake, and it is the dangerous kind: the
        # caller believes their sink is wired when the stack they passed decides
        # where evidence actually goes. Refuse rather than silently pick one.
        msg = (
            "build_agent received both middleware= and sink=/run_context=. A supplied "
            "middleware stack owns evidence routing, so sink=/run_context= would be "
            "silently ignored. Pass one or the other."
        )
        raise MissingEvidenceMiddleware(msg)

    if middleware is None:
        if sink is None or run_context is None:
            msg = "build_agent needs either middleware=[...] or both sink= and run_context="
            raise MissingEvidenceMiddleware(msg)
        # Section 4.2 order. EvidenceMiddleware first makes it outermost, so it
        # sees every tool call including ones ToolErrorMiddleware turns into an
        # error message rather than an exception.
        middleware = [
            EvidenceMiddleware(sink=sink, run_context=run_context),
            ToolErrorMiddleware(_tool_error_to_model),
            ModelCallLimitMiddleware(run_limit=run_limit, exit_behavior="end"),
        ]
    _require_evidence(middleware)

    return create_deep_agent(
        model=model,
        system_prompt=system_prompt if system_prompt is not None else load_prompt(),
        tools=list(tools),
        backend=backend,
        subagents=_audited_subagents(middleware, skills),
        skills=list(skills),
        permissions=permissions if permissions is not None else default_permissions(),
        interrupt_on=interrupt_on,
        middleware=list(middleware),
        name=name,
    )
