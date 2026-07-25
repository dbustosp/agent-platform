"""The MCP surface: exactly three read tools, and no way to write evidence.

Run in-process with the `mcp` package's own in-memory transport
(`mcp.shared.memory.create_connected_server_and_client_session`) — a real
client session over real protocol messages, no subprocess and no socket.

The async tests are driven by `asyncio.run` rather than a pytest plugin: the
suite must pass on a bare install, and `pytest-asyncio` is a dev extra.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from agent_platform.evidence.record import EvidenceRecord, PolicyDecision
from agent_platform.gateway.knowledge import YamlKnowledgeSource
from agent_platform.gateway.server import (
    DEFAULT_SERVER_NAME,
    create_gateway_server,
    serve_stdio,
)
from agent_platform.gateway.tools import READ_TOOL_NAMES, Gateway, read_only_policy
from agent_platform.identity import Principal, RunContext
from agent_platform.policy import AllowlistPolicy

T = TypeVar("T")

SERVICES = """\
services:
  - name: checkout-api
    owner_team: payments-platform
    owner_contact: "#payments-platform"
    tier: tier-1
    criticality: critical
    description: Payment authorisation.
    dependencies: [ledger-service]
"""

CONTROLS = """\
change_classes:
  - change_class: schema-migration
    description: DDL against a live store.
    controls:
      - control_id: CTL-DB-001
        title: Backward-compatible migration plan
        standard: ENG-STD-004
        requirement: Expand then contract.
"""

ASSERTIONS = """\
assertions:
  - subject: checkout-api
    statement: Latency budget is 250 ms p99.
    source: ADR-0114
    recorded_at: 2026-06-02
"""


class ListSink:
    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    def emit(self, record: EvidenceRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        pass


def run(coro_factory: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(coro_factory())


@pytest.fixture
def sink() -> ListSink:
    return ListSink()


@pytest.fixture
def knowledge(tmp_path: Path) -> YamlKnowledgeSource:
    directory = tmp_path / "knowledge"
    directory.mkdir()
    (directory / "services.yaml").write_text(SERVICES, encoding="utf-8")
    (directory / "controls.yaml").write_text(CONTROLS, encoding="utf-8")
    (directory / "assertions.yaml").write_text(ASSERTIONS, encoding="utf-8")
    return YamlKnowledgeSource.from_directory(directory)


@pytest.fixture
def context() -> RunContext:
    return RunContext.start(
        "review a migration",
        Principal(agent_principal="agent://local/pilot", delegated_by="tester"),
    )


@pytest.fixture
def gateway(knowledge: YamlKnowledgeSource, sink: ListSink, context: RunContext) -> Gateway:
    return Gateway(knowledge, read_only_policy(), sink, context)


@pytest.fixture
def server(gateway: Gateway) -> FastMCP:
    return create_gateway_server(gateway)


# -- Registration -------------------------------------------------------------


def test_server_registers_exactly_the_three_read_tools(server: FastMCP) -> None:
    names = {tool.name for tool in run(server.list_tools)}
    assert names == set(READ_TOOL_NAMES)


def test_record_evidence_is_absent_from_the_agent_facing_surface(
    server: FastMCP, gateway: Gateway
) -> None:
    """§5 Phase 1: the agent's gateway connection is read-only.

    The method exists on the `Gateway` object — the harness middleware calls it
    in-process — but it is not advertised over MCP, so an agent has no route to
    shape the record of what it did.
    """
    names = {tool.name for tool in run(server.list_tools)}
    assert "record_evidence" not in names
    assert callable(gateway.record_evidence)


def test_registered_tools_are_described_and_schema_d(server: FastMCP) -> None:
    """A tool a model cannot tell apart from another is a tool it will misuse."""
    by_name = {tool.name: tool for tool in run(server.list_tools)}
    expected_argument = {
        "lookup_service_context": "service",
        "get_control_requirements": "change_class",
        "recall": "subject",
    }
    for name, argument in expected_argument.items():
        tool = by_name[name]
        assert tool.description
        assert tool.inputSchema["required"] == [argument]
        assert tool.inputSchema["properties"][argument]["type"] == "string"


def test_server_carries_a_name_and_usage_instructions(server: FastMCP) -> None:
    assert server.name == DEFAULT_SERVER_NAME
    assert server.instructions is not None
    assert "authorised and logged" in server.instructions


def test_serve_stdio_is_importable_without_being_run() -> None:
    """The CLI's entrypoint helper exists and takes a configured gateway."""
    import inspect

    assert list(inspect.signature(serve_stdio).parameters) == ["gateway", "name"]


# -- Invocation ---------------------------------------------------------------


def test_tools_are_invocable_over_an_in_memory_session(server: FastMCP, sink: ListSink) -> None:
    async def exercise() -> dict[str, Any]:
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            assert {t.name for t in listed.tools} == set(READ_TOOL_NAMES)

            hit = await client.call_tool("lookup_service_context", {"service": "checkout-api"})
            miss = await client.call_tool("recall", {"subject": "nothing-recorded"})
            controls = await client.call_tool(
                "get_control_requirements", {"change_class": "schema-migration"}
            )
            return {
                "hit": hit.structuredContent,
                "miss": miss.structuredContent,
                "controls": controls.structuredContent,
                "errors": [hit.isError, miss.isError, controls.isError],
            }

    out = run(exercise)

    assert out["errors"] == [False, False, False]
    assert out["hit"]["found"] is True
    assert out["hit"]["ownership"]["team"] == "payments-platform"
    assert out["hit"]["criticality"] == "critical"
    assert out["miss"]["found"] is False
    assert out["miss"]["assertions"] == []
    assert out["controls"]["controls"][0]["control_id"] == "CTL-DB-001"

    assert [r.tool for r in sink.records] == [
        "lookup_service_context",
        "recall",
        "get_control_requirements",
    ]
    assert all(r.policy_decision is PolicyDecision.ALLOW for r in sink.records)


def test_a_tool_the_policy_refuses_fails_over_the_wire_and_is_logged(
    knowledge: YamlKnowledgeSource, sink: ListSink, context: RunContext
) -> None:
    """Registration does not grant permission. The policy still decides."""
    denied = Gateway(
        knowledge,
        AllowlistPolicy(allowed_tools=frozenset({"recall"})),
        sink,
        context,
    )
    server = create_gateway_server(denied)

    async def exercise() -> tuple[bool, str]:
        async with create_connected_server_and_client_session(server) as client:
            out = await client.call_tool("lookup_service_context", {"service": "checkout-api"})
            return bool(out.isError), out.content[0].text  # type: ignore[union-attr]

    is_error, message = run(exercise)

    assert is_error is True
    assert "policy denied" in message
    assert sink.records[-1].tool == "lookup_service_context"
    assert sink.records[-1].policy_decision is PolicyDecision.DENY


def test_the_gateway_package_does_not_reach_for_the_harness() -> None:
    """Import discipline, checked rather than asserted in a docstring.

    `agent_platform.gateway` may import the control plane and `mcp`. If it ever
    imports `deepagents`, the layer we own forever has taken a dependency on the
    layer we expect to throw away (P1, P5). §3.4's by-name bans are checked in
    the next test.
    """
    import ast

    import agent_platform.gateway as package

    banned = ("deepagents", "langchain", "langgraph", "agent_platform.harness")
    modules = sorted(Path(package.__file__).parent.glob("*.py"))
    assert modules, "the gateway package should contain modules to check"
    for module_path in modules:
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            assert not name.startswith(banned), f"{module_path.name} imports {name}"


def test_the_gateway_package_names_no_banned_backend() -> None:
    """§3.4's by-name bans, checked in the gateway too.

    `ContextHubBackend` and a bare `StoreBackend(` are the two constructions the
    plan bans because they look identical to the correct choice. `harness/` has
    its own guard; this one exists so the ban cannot be satisfied by moving the
    call site into the layer we own forever.
    """
    import re

    import agent_platform.gateway as package

    patterns = {
        "ContextHubBackend": re.compile(r"(?<![\w.])ContextHubBackend\s*\("),
        "StoreBackend": re.compile(r"(?<![\w.])StoreBackend\s*\("),
    }
    for module_path in sorted(Path(package.__file__).parent.glob("*.py")):
        source = module_path.read_text(encoding="utf-8")
        for label, pattern in patterns.items():
            assert not pattern.search(source), f"{module_path.name} constructs {label}"
