"""`EvidenceMiddleware` — the one piece Section 4.2 calls non-negotiable.

Section 9 rates a late evidence gap as the only Severe risk on the register, so
these tests are the closest thing this repository has to a control test. They
exercise the middleware directly rather than through an agent: end-to-end
coverage lives in `test_agent_assembly.py`, and a unit that only ever runs
inside a graph is a unit nobody can debug.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from agent_platform.evidence.hashing import hash_args, hash_result
from agent_platform.evidence.record import PolicyDecision
from agent_platform.evidence.sinks.memory import InMemorySink
from agent_platform.harness.middleware import (
    HARNESS_VERSION,
    EvidenceMiddleware,
    _gateway_tool_names,
)
from agent_platform.identity import Principal, RunContext

SECRET = "customer-4417-national-insurance-QQ123456C"


def make_request(name: str, args: dict | None = None) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": f"call_{name}"},
        tool=None,
        state={},
        runtime=None,  # type: ignore[arg-type]
    )


def make_context(**kwargs) -> RunContext:
    return RunContext.start(
        "find the owner of billing",
        Principal("agent://local/pilot", "danilo"),
        **kwargs,
    )


def ok(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(content="fine", tool_call_id=request.tool_call["id"])


def test_one_row_per_tool_call() -> None:
    sink = InMemorySink()
    mw = EvidenceMiddleware(sink, make_context(model_id="vertex:test"), skip_tools=())

    mw.wrap_tool_call(make_request("recall", {"subject": "billing"}), ok)
    mw.wrap_tool_call(make_request("recall", {"subject": "payments"}), ok)

    assert [r.tool for r in sink.records] == ["recall", "recall"]
    # Same tool, different arguments: the digests must differ, or `args_hash`
    # cannot answer the only question it exists to answer.
    assert sink.records[0].args_hash != sink.records[1].args_hash
    assert sink.records[0].model_id == "vertex:test"
    assert sink.records[0].harness_version == HARNESS_VERSION
    assert sink.records[0].policy_decision is PolicyDecision.ALLOW


def test_arguments_and_results_are_hashed_never_stored() -> None:
    """Section 6: hash rather than store, to keep data classification out of Phase 1."""
    sink = InMemorySink()
    mw = EvidenceMiddleware(sink, make_context(), skip_tools=())

    def leaky(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content=SECRET, tool_call_id=request.tool_call["id"])

    mw.wrap_tool_call(make_request("recall", {"subject": SECRET}), leaky)

    (record,) = sink.records
    assert record.args_hash == hash_args({"subject": SECRET})
    assert SECRET not in json.dumps(record.to_dict())


def test_result_hash_ignores_the_message_envelope() -> None:
    """Two identical calls must produce one digest, despite fresh message ids."""
    sink = InMemorySink()
    mw = EvidenceMiddleware(sink, make_context(), skip_tools=())

    mw.wrap_tool_call(make_request("recall", {"s": 1}), ok)
    mw.wrap_tool_call(make_request("recall", {"s": 1}), ok)

    first, second = sink.records
    assert first.result_hash == second.result_hash
    assert first.result_hash == hash_result({"content": "fine", "status": "success"})


def test_result_hash_is_stable_for_command_returning_tools() -> None:
    """`wrap_tool_call` returns `ToolMessage | Command`, and both are envelopes.

    `Command.update["messages"]` is rebuilt on every call with fresh random
    message ids. Digesting the `Command` as-is falls through to `repr()` and
    embeds them, so two identical calls hash differently — which is precisely
    the failure `test_result_hash_ignores_the_message_envelope` rules out for
    the `ToolMessage` half. deepagents' `write_todos` and `task` both return
    `Command`, so this is the live path, not a hypothetical one.
    """
    sink = InMemorySink()
    mw = EvidenceMiddleware(sink, make_context(), skip_tools=())

    def todos(request: ToolCallRequest) -> Command:
        # A fresh `id` per message, which is what LangGraph and deepagents
        # actually produce. Omitting it would leave `id=None` on every call and
        # make this test pass against the very bug it exists to catch.
        return Command(
            update={
                "todos": [{"content": "do a thing", "status": "pending"}],
                "messages": [
                    ToolMessage(
                        content="updated",
                        tool_call_id=request.tool_call["id"],
                        id=str(uuid.uuid4()),
                    )
                ],
            }
        )

    mw.wrap_tool_call(make_request("write_todos", {"todos": ["a"]}), todos)
    mw.wrap_tool_call(make_request("write_todos", {"todos": ["a"]}), todos)
    mw.wrap_tool_call(make_request("write_todos", {"todos": ["b"]}), todos)

    first, second, third = sink.records
    assert first.result_hash == second.result_hash
    # And it is still a digest of the payload, not a constant: a different
    # state update must still move the hash.
    assert first.args_hash != third.args_hash

    def other(_request: ToolCallRequest) -> Command:
        return Command(update={"todos": [], "messages": []})

    mw.wrap_tool_call(make_request("write_todos", {"todos": ["a"]}), other)
    assert sink.records[3].result_hash != first.result_hash


def test_a_langgraph_control_signal_is_not_recorded_as_an_outcome() -> None:
    """An interrupt means the tool has not finished, not that it failed.

    The call is replayed on resume and records its real result then, so
    recording here would both invent a failure and double-count the call.
    `GraphInterrupt` subclasses `Exception`, so a bare `except Exception`
    catches it — `ToolErrorMiddleware` guards the same signal for the same
    reason.
    """
    sink = InMemorySink()
    mw = EvidenceMiddleware(sink, make_context(), skip_tools=())

    def interrupts(_request: ToolCallRequest) -> ToolMessage:
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        mw.wrap_tool_call(make_request("danger", {"x": 1}), interrupts)
    assert list(sink.records) == []

    async def ainterrupts(_request: ToolCallRequest) -> ToolMessage:
        raise GraphInterrupt(())

    async def drive() -> None:
        with pytest.raises(GraphInterrupt):
            await mw.awrap_tool_call(make_request("danger", {"x": 1}), ainterrupts)

    asyncio.run(drive())
    assert list(sink.records) == []

    # The resumed call is what gets recorded, exactly once.
    mw.wrap_tool_call(make_request("danger", {"x": 1}), ok)
    assert [r.tool for r in sink.records] == ["danger"]


def test_a_raising_tool_still_produces_a_row_and_still_raises() -> None:
    """The failure is the interesting half of an audit trail.

    The middleware observes; it does not handle. Swallowing the exception here
    would silently change agent behaviour, which is exactly what P2 says
    middleware must not be able to do.
    """
    sink = InMemorySink()
    mw = EvidenceMiddleware(sink, make_context(), skip_tools=())

    def boom(_request: ToolCallRequest) -> ToolMessage:
        msg = "gateway unreachable"
        raise ConnectionError(msg)

    with pytest.raises(ConnectionError, match="gateway unreachable"):
        mw.wrap_tool_call(make_request("recall", {"subject": "billing"}), boom)

    (record,) = sink.records
    assert record.tool == "recall"
    assert record.result_hash == hash_result(
        {"__error__": "ConnectionError", "message": "gateway unreachable"}
    )


def test_a_failing_sink_does_not_break_the_agent_loop() -> None:
    """An evidence gap is severe; a dead agent produces no evidence at all."""

    class BrokenSink:
        def emit(self, record) -> None:  # noqa: ANN001
            msg = "BigQuery is having a day"
            raise RuntimeError(msg)

        def close(self) -> None:
            pass

    mw = EvidenceMiddleware(BrokenSink(), make_context(), skip_tools=())
    result = mw.wrap_tool_call(make_request("recall"), ok)
    assert result.content == "fine"


def test_skip_set_suppresses_gateway_owned_tools() -> None:
    """The gateway logs its own calls with the real policy decision (P2).

    Recording them here too would double-count every gateway row and attribute
    an `allow` this middleware is not entitled to make.
    """
    sink = InMemorySink()
    mw = EvidenceMiddleware(sink, make_context(), skip_tools={"recall"})

    assert mw.wrap_tool_call(make_request("recall"), ok).content == "fine"
    mw.wrap_tool_call(make_request("read_file"), ok)

    assert [r.tool for r in sink.records] == ["read_file"]


def test_skip_set_defaults_to_the_gateway_surface() -> None:
    """Resolved lazily so `harness` never hard-depends on `gateway` (P5).

    Asserting `skip_tools == _gateway_tool_names()` would restate the
    implementation and would still pass if the lazy import silently returned
    an empty set — i.e. if the de-duplication this exists for stopped
    happening. Name the tools and prove the behaviour instead.
    """
    from agent_platform.gateway import GATEWAY_TOOL_NAMES

    sink = InMemorySink()
    mw = EvidenceMiddleware(sink, make_context())

    assert mw.skip_tools == GATEWAY_TOOL_NAMES
    assert {"recall", "lookup_service_context", "get_control_requirements"} <= mw.skip_tools
    assert "read_file" not in mw.skip_tools

    mw.wrap_tool_call(make_request("recall"), ok)
    mw.wrap_tool_call(make_request("read_file"), ok)
    assert [r.tool for r in sink.records] == ["read_file"]


def test_gateway_names_resolve_without_a_module_scope_dependency() -> None:
    """The lazy import is the mechanism; an empty result means it broke."""
    import agent_platform.harness.middleware as mod

    assert "agent_platform.gateway" not in getattr(mod, "__dict__", {})
    assert _gateway_tool_names(), "gateway tool names resolved to nothing"


def test_subagent_lineage_comes_from_run_context_child() -> None:
    """Section 6's `parent_run_id` is a value passed down, not state we hope for."""
    sink = InMemorySink()
    parent_ctx = make_context()
    child_ctx = parent_ctx.child("summarise the dependency graph")

    EvidenceMiddleware(sink, parent_ctx, skip_tools=()).wrap_tool_call(make_request("recall"), ok)
    EvidenceMiddleware(sink, child_ctx, skip_tools=()).wrap_tool_call(make_request("recall"), ok)

    parent_row, child_row = sink.records
    assert parent_row.parent_run_id is None
    assert child_row.parent_run_id == parent_ctx.run_id
    assert child_row.run_id != parent_ctx.run_id
    assert child_row.intent == "summarise the dependency graph"
    assert child_row.agent_principal == parent_row.agent_principal


def test_async_path_matches_the_sync_path() -> None:
    """`awrap_tool_call` is a separate hook; a graph run on `ainvoke` uses it.

    Driven with `asyncio.run` rather than a plugin, because `pytest-asyncio`
    is not installed and the suite must run on a bare dev environment.
    """
    sink = InMemorySink()
    mw = EvidenceMiddleware(sink, make_context(), skip_tools={"recall"})

    async def aok(request: ToolCallRequest) -> ToolMessage:
        return ok(request)

    async def aboom(_request: ToolCallRequest) -> ToolMessage:
        msg = "async failure"
        raise ValueError(msg)

    async def drive() -> None:
        assert (await mw.awrap_tool_call(make_request("recall"), aok)).content == "fine"
        await mw.awrap_tool_call(make_request("read_file", {"path": "/skills/a"}), aok)
        with pytest.raises(ValueError, match="async failure"):
            await mw.awrap_tool_call(make_request("write_file"), aboom)

    asyncio.run(drive())

    assert [r.tool for r in sink.records] == ["read_file", "write_file"]
    assert sink.records[1].result_hash == hash_result(
        {"__error__": "ValueError", "message": "async failure"}
    )
