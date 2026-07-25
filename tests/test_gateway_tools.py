"""The Section 3.3 tool surface: shapes, misses, and one evidence row per call.

The shape assertions are not decoration. These dicts are what a model reads, and
`found` is the field that lets it tell "we hold nothing about this" apart from
"the gateway is broken" — a distinction it cannot make from an exception.

The evidence assertions are the P3 half: the row is written here, at the point
of action, carrying the run lineage and the principal that acted.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent_platform.evidence.hashing import hash_args, hash_result
from agent_platform.evidence.record import EvidenceRecord, PolicyDecision
from agent_platform.gateway.knowledge import (
    Assertion,
    ControlRequirements,
    ServiceContext,
    YamlKnowledgeSource,
)
from agent_platform.gateway.tools import (
    GATEWAY_TOOL_NAMES,
    READ_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    Gateway,
    UnknownGatewayTool,
    read_only_policy,
)
from agent_platform.identity import Principal, RunContext
from agent_platform.policy import AllowAllPolicy, AllowlistPolicy

SERVICES = """\
services:
  - name: checkout-api
    aliases: [checkout]
    owner_team: payments-platform
    owner_contact: "#payments-platform"
    tier: tier-1
    criticality: critical
    repo: github.com/acme-internal/checkout-api
    data_classification: pci
    description: Payment authorisation.
    dependencies: [ledger-service, postgres-checkout]
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
        evidence: Migration plan in the ticket.
"""

ASSERTIONS = """\
assertions:
  - subject: checkout-api
    statement: Latency budget is 250 ms p99.
    source: ADR-0114
    recorded_at: 2026-06-02
    confidence: high
"""


class ListSink:
    """Collects records in memory. Order is the order they were emitted."""

    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []
        self.closed = False

    def emit(self, record: EvidenceRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def knowledge(tmp_path: Path) -> YamlKnowledgeSource:
    directory = tmp_path / "knowledge"
    directory.mkdir()
    (directory / "services.yaml").write_text(SERVICES, encoding="utf-8")
    (directory / "controls.yaml").write_text(CONTROLS, encoding="utf-8")
    (directory / "assertions.yaml").write_text(ASSERTIONS, encoding="utf-8")
    return YamlKnowledgeSource.from_directory(directory)


@pytest.fixture
def sink() -> ListSink:
    return ListSink()


@pytest.fixture
def context() -> RunContext:
    return RunContext.start(
        "review the pending payment migration",
        Principal(agent_principal="agent://local/pilot", delegated_by="tester"),
        model_id="vertex/gemini-2.5-pro",
        harness_version="deepagents==0.6.12",
    )


@pytest.fixture
def gateway(knowledge: YamlKnowledgeSource, sink: ListSink, context: RunContext) -> Gateway:
    """A gateway permitted to use every tool, including the write-class one."""
    policy = AllowlistPolicy(allowed_tools=frozenset(GATEWAY_TOOL_NAMES))
    return Gateway(knowledge, policy, sink, context)


# -- Tool name constants ------------------------------------------------------


def test_tool_name_constants_describe_the_surface() -> None:
    assert sorted(READ_TOOL_NAMES) == [
        "get_control_requirements",
        "lookup_service_context",
        "recall",
    ]
    assert sorted(WRITE_TOOL_NAMES) == ["record_evidence"]
    assert GATEWAY_TOOL_NAMES == READ_TOOL_NAMES | WRITE_TOOL_NAMES


def test_read_only_policy_excludes_the_write_tool() -> None:
    """The agent-facing default cannot write evidence, only read knowledge."""
    assert read_only_policy().allowed_tools == READ_TOOL_NAMES
    assert "record_evidence" not in read_only_policy().allowed_tools


# -- Shapes -------------------------------------------------------------------


def test_lookup_service_context_returns_ownership_dependencies_criticality(
    gateway: Gateway,
) -> None:
    result = gateway.lookup_service_context("checkout-api")
    assert result["found"] is True
    assert result["service"] == "checkout-api"
    assert result["ownership"] == {
        "team": "payments-platform",
        "contact": "#payments-platform",
    }
    assert result["dependencies"] == ["ledger-service", "postgres-checkout"]
    assert result["criticality"] == "critical"
    assert result["data_classification"] == "pci"


def test_get_control_requirements_returns_controls_and_standards(
    gateway: Gateway,
) -> None:
    result = gateway.get_control_requirements("schema-migration")
    assert result["found"] is True
    assert result["standards"] == ["ENG-STD-004"]
    assert result["controls"][0]["control_id"] == "CTL-DB-001"
    assert result["controls"][0]["standard"] == "ENG-STD-004"


def test_recall_returns_assertions_with_their_sources(gateway: Gateway) -> None:
    result = gateway.recall("checkout-api")
    assert result["found"] is True
    assert result["count"] == 1
    assert result["assertions"][0]["source"] == "ADR-0114"
    assert result["assertions"][0]["subject"] == "checkout-api"


def test_results_are_json_safe(gateway: Gateway) -> None:
    """MCP serialises these. A dataclass leaking through breaks the transport."""
    for payload in (
        gateway.lookup_service_context("checkout-api"),
        gateway.get_control_requirements("schema-migration"),
        gateway.recall("checkout-api"),
    ):
        json.loads(json.dumps(payload))


# -- Misses -------------------------------------------------------------------


def test_unknown_service_is_a_clean_miss(gateway: Gateway, sink: ListSink) -> None:
    result = gateway.lookup_service_context("does-not-exist")
    assert result["found"] is False
    assert result["service"] == "does-not-exist"
    assert "does-not-exist" in result["detail"]
    # A miss is a successful, allowed call and is logged as one.
    assert sink.records[-1].policy_decision is PolicyDecision.ALLOW


def test_unknown_change_class_is_a_clean_miss(gateway: Gateway) -> None:
    result = gateway.get_control_requirements("interpretive-dance")
    assert result["found"] is False
    assert result["change_class"] == "interpretive-dance"
    assert "controls" not in result


def test_unknown_subject_recalls_nothing(gateway: Gateway) -> None:
    result = gateway.recall("nobody-has-said-anything-about-this")
    assert result == {
        "subject": "nobody-has-said-anything-about-this",
        "found": False,
        "count": 0,
        "assertions": [],
    }


# -- Evidence -----------------------------------------------------------------


def test_each_read_call_emits_exactly_one_row(gateway: Gateway, sink: ListSink) -> None:
    gateway.lookup_service_context("checkout-api")
    gateway.get_control_requirements("schema-migration")
    gateway.recall("checkout-api")
    assert [r.tool for r in sink.records] == [
        "lookup_service_context",
        "get_control_requirements",
        "recall",
    ]


def test_row_hashes_the_arguments_and_the_result(gateway: Gateway, sink: ListSink) -> None:
    payload = gateway.lookup_service_context("checkout-api")
    row = sink.records[-1]
    assert row.args_hash == hash_args({"service": "checkout-api"})
    assert row.result_hash == hash_result(payload)
    # Hash, not payload (§6): the service name must not appear in the row.
    assert "checkout-api" not in str(row.to_dict())


def test_row_carries_principal_and_run_lineage(
    knowledge: YamlKnowledgeSource, sink: ListSink, context: RunContext
) -> None:
    child = context.child("check the ledger dependency")
    policy = AllowlistPolicy(allowed_tools=frozenset(READ_TOOL_NAMES))
    subagent = Gateway(knowledge, policy, sink, child)

    subagent.recall("checkout-api")

    row = sink.records[-1]
    assert row.run_id == child.run_id
    assert row.parent_run_id == context.run_id
    assert row.agent_principal == "agent://local/pilot"
    assert row.delegated_by == "tester"
    assert row.intent == "check the ledger dependency"
    assert row.model_id == "vertex/gemini-2.5-pro"
    assert row.harness_version == "deepagents==0.6.12"
    assert row.policy_decision is PolicyDecision.ALLOW
    assert row.timestamp.tzinfo is not None


def test_with_context_shares_policy_and_sink(
    gateway: Gateway, sink: ListSink, context: RunContext
) -> None:
    derived = gateway.with_context(context.child())
    derived.recall("checkout-api")
    assert derived.context.parent_run_id == context.run_id
    assert len(sink.records) == 1


def test_record_evidence_writes_the_record_it_was_handed(gateway: Gateway, sink: ListSink) -> None:
    """The gateway does not re-author a caller's row — it would be a copy (P3)."""
    handed = EvidenceRecord(
        run_id="run_from_the_harness",
        agent_principal="agent://local/pilot",
        delegated_by="tester",
        intent="edit a file",
        tool="write_file",
        args_hash="sha256:abc",
        policy_decision=PolicyDecision.ALLOW,
        result_hash="sha256:def",
        harness_version="deepagents==0.6.12",
    )

    result = gateway.record_evidence(handed)

    assert result["recorded"] is True
    assert result["run_id"] == "run_from_the_harness"
    assert sink.records == [handed]


def test_record_evidence_accepts_a_serialised_record(gateway: Gateway, sink: ListSink) -> None:
    """A row that crossed a process boundary arrives as a dict, not an object."""
    original = EvidenceRecord(
        run_id="run_serialised",
        agent_principal="agent://local/pilot",
        delegated_by="tester",
        intent="list files",
        tool="ls",
        args_hash="sha256:abc",
        policy_decision=PolicyDecision.ALLOW,
    )
    gateway.record_evidence(original.to_dict())
    assert sink.records[0] == original


def test_every_call_produces_exactly_one_row(gateway: Gateway, sink: ListSink) -> None:
    """The invariant that makes the log countable, write-class tool included."""
    gateway.lookup_service_context("checkout-api")
    gateway.recall("checkout-api")
    gateway.record_evidence(
        EvidenceRecord(
            run_id="run_x",
            agent_principal="a",
            delegated_by="h",
            intent="i",
            tool="t",
            args_hash="sha256:abc",
            policy_decision=PolicyDecision.ALLOW,
        )
    )
    assert len(sink.records) == 3


def test_a_failing_tool_body_still_leaves_a_row(
    knowledge: YamlKnowledgeSource, sink: ListSink, context: RunContext
) -> None:
    """An errored call is still something the agent did."""

    class ExplodingKnowledge:
        def service_context(self, service: str) -> ServiceContext | None:
            raise RuntimeError("knowledge graph unreachable")

        def control_requirements(self, change_class: str) -> ControlRequirements | None:
            return None

        def assertions(self, subject: str) -> tuple[Assertion, ...]:
            return ()

    gateway = Gateway(
        ExplodingKnowledge(),
        AllowlistPolicy(allowed_tools=frozenset(READ_TOOL_NAMES)),
        sink,
        context,
    )

    with pytest.raises(RuntimeError, match="knowledge graph unreachable"):
        gateway.lookup_service_context("checkout-api")

    row = sink.records[-1]
    assert row.tool == "lookup_service_context"
    assert row.policy_decision is PolicyDecision.ALLOW
    assert row.result_hash == hash_result(
        {"error": "RuntimeError", "message": "knowledge graph unreachable"}
    )


def test_a_gateway_wired_to_allow_all_says_so_out_loud(
    knowledge: YamlKnowledgeSource,
    sink: ListSink,
    context: RunContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`AllowAllPolicy` documents that the gateway warns. Make that true.

    A gateway with no control at the tool boundary is the one configuration an
    operator must never reach by accident, and a promise kept only in a
    docstring is not a warning anyone will see.
    """
    with caplog.at_level(logging.WARNING, logger="agent_platform.gateway.tools"):
        Gateway(knowledge, AllowAllPolicy(), sink, context)

    assert any(
        record.levelno == logging.WARNING and "AllowAllPolicy" in record.getMessage()
        for record in caplog.records
    )


def test_the_normal_policy_is_silent(
    knowledge: YamlKnowledgeSource,
    sink: ListSink,
    context: RunContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning is only worth anything if it is not always on."""
    with caplog.at_level(logging.WARNING, logger="agent_platform.gateway.tools"):
        Gateway(knowledge, read_only_policy(), sink, context)

    assert caplog.records == []


def test_the_tool_surface_imports_without_the_mcp_extra() -> None:
    """P5, and the reason `__init__.py` does not re-export `server`.

    `mcp` is an optional extra. A process that only wants to call the tools —
    the harness, a test, a second transport — must be able to import them on a
    bare install. Run in a subprocess so blocking the module cannot leak into
    the rest of the session.
    """
    program = textwrap.dedent(
        """
        import builtins, sys
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "mcp" or name.startswith("mcp."):
                raise ModuleNotFoundError("No module named 'mcp'")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = blocked
        import agent_platform.gateway as gateway
        assert gateway.Gateway is not None
        assert "mcp" not in sys.modules
        try:
            import agent_platform.gateway.server  # noqa: F401
        except ModuleNotFoundError:
            print("OK")
        else:
            raise AssertionError("server.py imported without mcp installed")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK"


def test_allowlisted_but_unimplemented_tool_is_a_configuration_error(
    knowledge: YamlKnowledgeSource, sink: ListSink, context: RunContext
) -> None:
    gateway = Gateway(
        knowledge,
        AllowlistPolicy(allowed_tools=frozenset({"delete_production"})),
        sink,
        context,
    )
    with pytest.raises(UnknownGatewayTool):
        gateway.call("delete_production", {})
    assert sink.records[-1].tool == "delete_production"
    assert sink.records[-1].policy_decision is PolicyDecision.ALLOW
