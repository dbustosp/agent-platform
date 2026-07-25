"""Bridge the gateway's tool surface into the harness — the `gateway_mcp_tools` of §4.2.

Two ways to reach the same three read tools, and the difference matters:

- `gateway_tools_over_mcp()` is what §5 Phase 1 actually specifies — "one MCP
  connection to the gateway". The harness talks to a gateway *process* over
  stdio and knows nothing about our Python objects. This is the path that proves
  P1: a second harness gets the same tools by speaking the same protocol.
- `gateway_tools_in_process()` calls `Gateway.call` directly. Same policy check,
  same evidence rows, no subprocess. It exists for tests and for the local loop
  where spawning a server per run is pure latency.

The in-process path is a convenience, not a shortcut around anything: policy is
enforced inside `Gateway.call`, so both paths get identical enforcement. What
in-process skips is the transport, and the transport is the part we are least
worried about being wrong.

This module lives in `harness/` because it imports `langchain_core`. The gateway
itself must stay installable without the framework (P5).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from agent_platform.gateway import READ_TOOL_NAMES, Gateway
from agent_platform.policy import PolicyDenied

#: Argument name for each read tool, in the order the gateway declares them.
_READ_TOOL_ARGS: dict[str, str] = {
    "lookup_service_context": "service",
    "get_control_requirements": "change_class",
    "recall": "subject",
}

_DESCRIPTIONS: dict[str, str] = {
    "lookup_service_context": (
        "Look up the owning team, dependencies and criticality tier of an internal "
        "service or repo. Use this before reasoning about a service you have not "
        "been told about."
    ),
    "get_control_requirements": (
        "Return the controls and standards that apply to a class of change "
        "(for example: schema migration, dependency upgrade, access change)."
    ),
    "recall": (
        "Retrieve prior recorded assertions about a subject — previous decisions, "
        "known gaps, documented exceptions. Use this instead of assuming."
    ),
}


def _denial_payload(exc: PolicyDenied) -> dict[str, Any]:
    """Turn a boundary refusal into something the model can read and adapt to.

    The refusal has already happened and has already been recorded by the
    gateway; the tool body never ran. Returning it as data rather than raising
    keeps a denied call from looking like a transport fault, which is what makes
    a model retry it forever.
    """
    return {
        "status": "denied",
        "tool": exc.tool,
        "reason": exc.result.reason,
        "policy_decision": str(exc.result.decision),
    }


def gateway_tools_in_process(
    gateway: Gateway,
    *,
    tools: Sequence[str] | None = None,
) -> list[BaseTool]:
    """Wrap gateway read tools as LangChain tools that call `Gateway.call` directly."""
    names = list(tools) if tools is not None else sorted(READ_TOOL_NAMES)
    unknown = set(names) - set(_READ_TOOL_ARGS)
    if unknown:
        msg = (
            f"not gateway read tools: {sorted(unknown)}. "
            f"Phase 1 exposes only {sorted(_READ_TOOL_ARGS)} to the model."
        )
        raise ValueError(msg)

    built: list[BaseTool] = []
    for name in names:
        arg = _READ_TOOL_ARGS[name]

        def _call(_name: str = name, _arg: str = arg, **kwargs: Any) -> Any:
            try:
                return gateway.call(_name, {_arg: kwargs.get(_arg, "")})
            except PolicyDenied as exc:
                return _denial_payload(exc)

        # A distinct signature per tool so the model sees the right parameter name.
        _call.__name__ = name
        built.append(
            StructuredTool.from_function(
                func=_call,
                name=name,
                description=_DESCRIPTIONS[name],
                args_schema={
                    "type": "object",
                    "properties": {arg: {"type": "string"}},
                    "required": [arg],
                },
            )
        )
    return built


def stdio_connection(
    *,
    command: str | None = None,
    args: Sequence[str] | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    run_context: Any = None,
) -> dict[str, Any]:
    """Describe how to launch the gateway as an MCP server over stdio.

    Defaults to this interpreter running the CLI, so the connection works from a
    checkout without the console script being on PATH.

    Deliberately *not* `python -m agent_platform.gateway.server`: that module
    refuses to run standalone because picking an evidence sink is a deployment
    decision it must not make for you. The CLI is the composition root that
    reads the environment and makes it.

    Pass `run_context` to hand the gateway this task's identity. Without it the
    subprocess starts its own run, and because a sessionless MCP tool spawns a
    fresh process *per call*, every call would land under a different `run_id` —
    which is precisely what Section 6 says must not happen.
    """
    import sys

    if run_context is not None:
        from agent_platform.config import run_context_env

        env = run_context_env(run_context, base=env)

    return {
        "transport": "stdio",
        "command": command or sys.executable,
        "args": (
            list(args) if args is not None else ["-m", "agent_platform.cli", "gateway", "serve"]
        ),
        "cwd": str(cwd) if cwd is not None else None,
        "env": env,
    }


async def agateway_tools_over_mcp(connection: dict[str, Any] | None = None) -> list[BaseTool]:
    """Load the gateway's advertised tools over a real MCP stdio connection.

    Passing `session=None` makes each tool call open its own short-lived session,
    which is what lets a synchronous CLI use these without holding a connection
    open across the whole run.
    """
    from langchain_mcp_adapters.tools import load_mcp_tools

    return await load_mcp_tools(None, connection=connection or stdio_connection())


def gateway_tools_over_mcp(connection: dict[str, Any] | None = None) -> list[BaseTool]:
    """Synchronous wrapper around `agateway_tools_over_mcp`."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(agateway_tools_over_mcp(connection))
    msg = (
        "gateway_tools_over_mcp() cannot be called from inside a running event "
        "loop; await agateway_tools_over_mcp() instead"
    )
    raise RuntimeError(msg)
