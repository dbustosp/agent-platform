"""P2 — enforce at the tool boundary, never in the prompt or the loop.

*Test: can a control be bypassed by changing the system prompt or adding custom
middleware? If yes, it is not a control.*

This file is that test made executable, and it is the reason the policy check
lives in `Gateway.call` rather than in harness middleware. Four properties:

1. **Default-deny holds.** An empty allowlist refuses everything, including
   tool names the gateway does not implement.
2. **A refusal does not execute.** Proved by counting: the knowledge source
   records every call it receives, and a denied tool leaves the counter at zero.
   "The tool returned nothing" and "the tool never ran" are different claims,
   and only the second one is a control.
3. **A refusal is still evidence.** "Did it ever try?" is the question an audit
   opens with, and a log that only holds successes cannot answer it.
4. **Nothing a caller passes in changes the decision.** Identity comes from the
   gateway's run context, not from the argument dict.
"""

from __future__ import annotations

import pytest

from agent_platform.evidence.hashing import EMPTY, hash_args
from agent_platform.evidence.record import EvidenceRecord, PolicyDecision
from agent_platform.gateway.knowledge import (
    Assertion,
    ControlRequirements,
    ServiceContext,
)
from agent_platform.gateway.tools import READ_TOOL_NAMES, Gateway
from agent_platform.identity import Principal, RunContext
from agent_platform.policy import (
    AllowlistPolicy,
    PolicyDenied,
    PolicyRequest,
    PolicyResult,
)


class CountingKnowledge:
    """A knowledge source that refuses to be silent about being touched.

    Every method records the call. This is the instrument for property 2: if a
    denied tool call increments any counter, the body ran and the deny was
    cosmetic.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def service_context(self, service: str) -> ServiceContext | None:
        self.calls.append(("service_context", service))
        return ServiceContext(
            name=service,
            owner_team="payments-platform",
            owner_contact="#payments-platform",
            criticality="critical",
        )

    def control_requirements(self, change_class: str) -> ControlRequirements | None:
        self.calls.append(("control_requirements", change_class))
        return ControlRequirements(change_class=change_class, description="")

    def assertions(self, subject: str) -> tuple[Assertion, ...]:
        self.calls.append(("assertions", subject))
        return (Assertion(subject=subject, statement="something", source="somewhere"),)


class ListSink:
    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    def emit(self, record: EvidenceRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        pass


class RecordingPolicy:
    """Wraps a policy and keeps the requests it was asked to decide."""

    def __init__(self, inner: AllowlistPolicy) -> None:
        self.inner = inner
        self.requests: list[PolicyRequest] = []

    def evaluate(self, request: PolicyRequest) -> PolicyResult:
        self.requests.append(request)
        return self.inner.evaluate(request)


@pytest.fixture
def knowledge() -> CountingKnowledge:
    return CountingKnowledge()


@pytest.fixture
def sink() -> ListSink:
    return ListSink()


@pytest.fixture
def context() -> RunContext:
    return RunContext.start(
        "investigate an incident",
        Principal(agent_principal="agent://local/pilot", delegated_by="tester"),
    )


def build(
    knowledge: CountingKnowledge,
    sink: ListSink,
    context: RunContext,
    policy: object,
) -> Gateway:
    return Gateway(knowledge, policy, sink, context)  # type: ignore[arg-type]


# -- 1. Default-deny ----------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("lookup_service_context", {"service": "checkout-api"}),
        ("get_control_requirements", {"change_class": "schema-migration"}),
        ("recall", {"subject": "checkout-api"}),
        ("record_evidence", {"record": {}}),
        ("some_tool_nobody_defined", {}),
    ],
)
def test_empty_allowlist_denies_everything(
    knowledge: CountingKnowledge,
    sink: ListSink,
    context: RunContext,
    tool: str,
    args: dict[str, object],
) -> None:
    gateway = build(knowledge, sink, context, AllowlistPolicy())

    with pytest.raises(PolicyDenied):
        gateway.call(tool, args)

    assert knowledge.calls == []
    assert sink.records[-1].policy_decision is PolicyDecision.DENY


def test_an_unknown_tool_name_is_refused_before_it_is_resolved(
    knowledge: CountingKnowledge, sink: ListSink, context: RunContext
) -> None:
    """Policy runs before handler lookup.

    A probe for a tool that does not exist gets the same refusal as a probe for
    one that does, so the error shape cannot be used to enumerate the surface.
    """
    gateway = build(knowledge, sink, context, AllowlistPolicy(allowed_tools=READ_TOOL_NAMES))

    with pytest.raises(PolicyDenied):
        gateway.call("drop_database", {})

    assert sink.records[-1].tool == "drop_database"
    assert sink.records[-1].policy_decision is PolicyDecision.DENY


def test_principal_allowlist_denies_an_unrecognised_agent(
    knowledge: CountingKnowledge, sink: ListSink
) -> None:
    context = RunContext.start(
        "investigate an incident",
        Principal(agent_principal="agent://someone-elses/agent", delegated_by="tester"),
    )
    policy = AllowlistPolicy(
        allowed_tools=READ_TOOL_NAMES,
        allowed_principals=frozenset({"agent://local/pilot"}),
    )
    gateway = build(knowledge, sink, context, policy)

    with pytest.raises(PolicyDenied, match="not on the gateway allowlist"):
        gateway.recall("checkout-api")

    assert knowledge.calls == []


# -- 2. A refusal does not execute --------------------------------------------


def test_denied_read_tool_never_touches_the_knowledge_source(
    knowledge: CountingKnowledge, sink: ListSink, context: RunContext
) -> None:
    """The deny is real, not cosmetic: the body did not run."""
    policy = AllowlistPolicy(allowed_tools=frozenset({"recall"}))
    gateway = build(knowledge, sink, context, policy)

    with pytest.raises(PolicyDenied):
        gateway.lookup_service_context("checkout-api")
    assert knowledge.calls == []

    # The allowlisted tool proves the counter would have moved if it had run.
    gateway.recall("checkout-api")
    assert knowledge.calls == [("assertions", "checkout-api")]


def test_denied_write_tool_never_reaches_the_sink(
    knowledge: CountingKnowledge, sink: ListSink, context: RunContext
) -> None:
    """A refused `record_evidence` writes the refusal, not the handed record."""
    gateway = build(knowledge, sink, context, AllowlistPolicy(allowed_tools=READ_TOOL_NAMES))
    smuggled = EvidenceRecord(
        run_id="run_attacker",
        agent_principal="agent://local/pilot",
        delegated_by="tester",
        intent="rewrite history",
        tool="something_that_never_happened",
        args_hash="sha256:abc",
        policy_decision=PolicyDecision.ALLOW,
    )

    with pytest.raises(PolicyDenied):
        gateway.record_evidence(smuggled)

    assert smuggled not in sink.records
    assert [r.tool for r in sink.records] == ["record_evidence"]
    assert sink.records[0].policy_decision is PolicyDecision.DENY


# -- 3. A refusal is still evidence -------------------------------------------


def test_denial_writes_a_complete_row(
    knowledge: CountingKnowledge, sink: ListSink, context: RunContext
) -> None:
    gateway = build(knowledge, sink, context, AllowlistPolicy())

    with pytest.raises(PolicyDenied):
        gateway.lookup_service_context("checkout-api")

    assert len(sink.records) == 1
    row = sink.records[0]
    assert row.tool == "lookup_service_context"
    assert row.policy_decision is PolicyDecision.DENY
    assert row.args_hash == hash_args({"service": "checkout-api"})
    assert row.result_hash == EMPTY, "no body ran, so there is no result to hash"
    assert row.run_id == context.run_id
    assert row.agent_principal == "agent://local/pilot"
    assert row.delegated_by == "tester"
    assert row.intent == "investigate an incident"


def test_the_row_is_written_before_the_refusal_is_raised(
    knowledge: CountingKnowledge, context: RunContext
) -> None:
    """Order matters: an exception must not be able to skip the record."""
    seen: list[str] = []

    class OrderingSink:
        def emit(self, record: EvidenceRecord) -> None:
            seen.append("emit")

        def close(self) -> None:
            pass

    gateway = build(knowledge, OrderingSink(), context, AllowlistPolicy())  # type: ignore[arg-type]
    try:
        gateway.recall("checkout-api")
    except PolicyDenied:
        seen.append("raise")

    assert seen == ["emit", "raise"]


# -- Escalation ---------------------------------------------------------------


def test_escalation_returns_an_approval_request_without_executing(
    knowledge: CountingKnowledge, sink: ListSink, context: RunContext
) -> None:
    policy = AllowlistPolicy(
        allowed_tools=READ_TOOL_NAMES,
        escalate_tools=frozenset({"lookup_service_context"}),
    )
    gateway = build(knowledge, sink, context, policy)

    result = gateway.lookup_service_context("checkout-api")

    assert result["status"] == "approval_required"
    assert result["tool"] == "lookup_service_context"
    assert "requires approval" in result["reason"]
    assert knowledge.calls == [], "escalation must not run the tool body"

    row = sink.records[-1]
    assert row.policy_decision is PolicyDecision.ESCALATE
    assert row.result_hash == EMPTY


def test_escalation_carries_the_approval_reference_onto_the_row(
    knowledge: CountingKnowledge, sink: ListSink, context: RunContext
) -> None:
    class ApprovalPolicy:
        def evaluate(self, request: PolicyRequest) -> PolicyResult:
            return PolicyResult(
                PolicyDecision.ESCALATE,
                "write-class action",
                approval_ref="APR-4471",
            )

    gateway = build(knowledge, sink, context, ApprovalPolicy())
    result = gateway.recall("checkout-api")

    assert result["approval_ref"] == "APR-4471"
    assert sink.records[-1].approval_ref == "APR-4471"


# -- 4. Nothing a caller passes in changes the decision -----------------------


@pytest.mark.parametrize(
    "smuggled",
    [
        {"principal": "agent://root/superuser"},
        {"agent_principal": "agent://root/superuser", "delegated_by": "ceo"},
        {"policy_decision": "allow"},
        {"policy": None},
        {"_policy": "AllowAllPolicy"},
        {"approval_ref": "APR-0001"},
        {"__class__": "Gateway"},
    ],
)
def test_arguments_cannot_talk_the_gateway_into_an_allow(
    knowledge: CountingKnowledge,
    sink: ListSink,
    context: RunContext,
    smuggled: dict[str, object],
) -> None:
    gateway = build(knowledge, sink, context, AllowlistPolicy())

    with pytest.raises(PolicyDenied):
        gateway.call("lookup_service_context", {"service": "checkout-api", **smuggled})

    assert knowledge.calls == []
    assert sink.records[-1].policy_decision is PolicyDecision.DENY


def test_policy_always_sees_the_gateways_own_principal(
    knowledge: CountingKnowledge, sink: ListSink, context: RunContext
) -> None:
    """Identity comes from the connection, never from the argument dict.

    A smuggled `principal` reaches the policy as *data* — an engine may inspect
    it, and it changes the args_hash — but the identity the decision is taken
    against is the one bound to this gateway.
    """
    policy = RecordingPolicy(AllowlistPolicy(allowed_tools=READ_TOOL_NAMES))
    gateway = build(knowledge, sink, context, policy)

    gateway.recall("checkout-api")
    assert policy.requests[0].tool == "recall"
    assert policy.requests[0].principal is context.principal

    impostor = Principal("agent://root/superuser", "ceo")
    with pytest.raises(TypeError):
        gateway.call("recall", {"subject": "checkout-api", "principal": impostor})

    assert policy.requests[1].principal is context.principal
    assert policy.requests[1].args["principal"] is impostor
    # Rejected by the handler signature, and still recorded as an attempt.
    assert sink.records[-1].tool == "recall"
    assert sink.records[-1].agent_principal == "agent://local/pilot"


def test_the_public_tool_methods_take_only_their_own_arguments() -> None:
    """No `policy=`, no `principal=`, no `**kwargs` escape hatch on the surface."""
    import inspect

    expected = {
        "lookup_service_context": ["self", "service"],
        "get_control_requirements": ["self", "change_class"],
        "recall": ["self", "subject"],
        "record_evidence": ["self", "record"],
    }
    for name, parameters in expected.items():
        signature = inspect.signature(getattr(Gateway, name))
        assert list(signature.parameters) == parameters
        assert all(
            p.kind is not inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
        )


def test_the_tool_bodies_are_not_public(
    knowledge: CountingKnowledge, sink: ListSink, context: RunContext
) -> None:
    """Every public callable on the gateway routes through `call`.

    A public method that reaches a handler directly would be a second door into
    the tool bodies, and a control with two doors is a control with none.
    """
    gateway = build(knowledge, sink, context, AllowlistPolicy())
    public = {
        name
        for name in dir(gateway)
        if not name.startswith("_") and callable(getattr(gateway, name))
    }
    assert public == {
        "call",
        "with_context",
        "lookup_service_context",
        "get_control_requirements",
        "recall",
        "record_evidence",
    }


def test_removing_every_middleware_changes_nothing(
    knowledge: CountingKnowledge, sink: ListSink, context: RunContext
) -> None:
    """P2's literal test.

    There is no harness in this file — no `deepagents`, no middleware, no system
    prompt. The refusal still happens, because the decision was never taken
    there. Nothing a harness does or stops doing can reach this code path.
    """
    gateway = build(knowledge, sink, context, AllowlistPolicy())

    with pytest.raises(PolicyDenied):
        gateway.recall("checkout-api")

    assert knowledge.calls == []
    assert sink.records[-1].policy_decision is PolicyDecision.DENY
