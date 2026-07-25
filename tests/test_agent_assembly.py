"""`build_agent` — Section 4.2 assembled, driven end to end with no API key.

Two things are being proved here. First, that the whole stack actually runs:
scripted model, composite backend, Git skills, evidence middleware, real
LangGraph execution. Second, that it cannot be assembled without evidence,
because the plan calls that non-negotiable and a convention is not a control.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolErrorMiddleware
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from agent_platform.evidence.hashing import hash_args
from agent_platform.evidence.record import PolicyDecision
from agent_platform.evidence.sinks.memory import InMemorySink
from agent_platform.harness.agent import (
    MissingEvidenceMiddleware,
    build_agent,
    default_permissions,
    load_prompt,
)
from agent_platform.harness.backends import build_backend
from agent_platform.harness.middleware import HARNESS_VERSION, EvidenceMiddleware
from agent_platform.harness.models import build_model
from agent_platform.harness.skills_store import GitSkillStore
from agent_platform.harness.testing import ScriptedChatModel
from agent_platform.identity import Principal, RunContext


@tool
def widget_owner(service: str) -> str:
    """Return the owning team for a service."""
    return f"{service} is owned by team-platform"


SKILL_MD = "---\nname: web-research\ndescription: How to research a topic\n---\n\nDo the thing.\n"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    skill = tmp_path / "web-research"
    skill.mkdir()
    (skill / "SKILL.md").write_text(SKILL_MD)
    return tmp_path


@pytest.fixture
def backend(checkout: Path):
    return build_backend(skill_store=GitSkillStore(checkout))


@pytest.fixture
def run_context() -> RunContext:
    return RunContext.start(
        "find the owner of billing",
        Principal("agent://local/pilot", "danilo"),
        model_id="fake:scripted",
    )


def test_a_scripted_tool_call_lands_exactly_one_evidence_row(backend, run_context) -> None:
    """The Phase 1 exit criterion in miniature: evidence rows accumulating."""
    sink = InMemorySink()
    model = ScriptedChatModel.from_script(
        ("widget_owner", {"service": "billing"}),
        final="team-platform owns it.",
    )
    agent = build_agent(
        model=model,
        backend=backend,
        tools=[widget_owner],
        sink=sink,
        run_context=run_context,
    )

    result = agent.invoke({"messages": [{"role": "user", "content": "who owns billing?"}]})

    assert result["messages"][-1].content == "team-platform owns it."
    (record,) = sink.records
    assert record.tool == "widget_owner"
    assert record.args_hash == hash_args({"service": "billing"})
    assert record.result_hash is not None
    assert record.run_id == run_context.run_id
    assert record.agent_principal == "agent://local/pilot"
    assert record.delegated_by == "danilo"
    assert record.intent == "find the owner of billing"
    assert record.model_id == "fake:scripted"
    assert record.harness_version == HARNESS_VERSION
    assert record.policy_decision is PolicyDecision.ALLOW


def test_the_agent_can_read_a_skill_and_cannot_overwrite_it(backend, checkout, run_context) -> None:
    """P4 from both sides: the harness reads semantic state, never owns it.

    A denial message is not evidence of a denial — a backend that wrote the
    file and *then* reported "permission denied" would satisfy the string
    assertion. The checkout is what the claim is actually about, so the
    checkout is what gets asserted on.
    """
    sink = InMemorySink()
    model = ScriptedChatModel.from_script(
        ("read_file", {"file_path": "/skills/web-research/SKILL.md"}),
        ("write_file", {"file_path": "/skills/web-research/SKILL.md", "content": "mine"}),
        ("write_file", {"file_path": "/skills/new-skill/SKILL.md", "content": "mine"}),
        final="done",
    )
    agent = build_agent(model=model, backend=backend, sink=sink, run_context=run_context)

    result = agent.invoke({"messages": [{"role": "user", "content": "go"}]})

    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert "name: web-research" in tool_messages[0].content
    assert "permission denied" in tool_messages[1].content
    assert "permission denied" in tool_messages[2].content
    # The tool bodies did not run: Git's copy is byte-identical and the skill
    # the agent tried to invent does not exist.
    assert (checkout / "web-research" / "SKILL.md").read_text() == SKILL_MD
    assert not (checkout / "new-skill").exists()
    assert sorted(p.name for p in checkout.iterdir()) == ["web-research"]
    # All three attempts are recorded. The refused ones are the rows worth having.
    assert [r.tool for r in sink.records] == ["read_file", "write_file", "write_file"]


def test_a_delegated_subagent_still_produces_evidence(backend, run_context) -> None:
    """`create_deep_agent` ships a `task` tool; the subagent behind it is ours.

    deepagents builds the general-purpose subagent's middleware stack itself
    and does not copy the caller's into it, so without an explicit spec the
    subagent's tool calls run with no evidence row at all — an audit hole the
    parent's single `task` row hides rather than fills. `called` is what proves
    the tool body ran: without it this test could pass on an agent that never
    delegated.
    """
    called: list[str] = []

    @tool
    def secret_lookup(subject: str) -> str:
        """Look a subject up."""
        called.append(subject)
        return f"answer for {subject}"

    sink = InMemorySink()
    model = ScriptedChatModel.from_script(
        ("task", {"description": "look up billing", "subagent_type": "general-purpose"}),
        ("secret_lookup", {"subject": "billing"}),
        AIMessage(content="subagent done"),
        final="main done",
    )
    agent = build_agent(
        model=model,
        backend=backend,
        tools=[secret_lookup],
        sink=sink,
        run_context=run_context,
    )

    agent.invoke({"messages": [{"role": "user", "content": "go"}]})

    assert called == ["billing"], "the subagent never ran the tool; test proves nothing"
    by_tool = {r.tool: r for r in sink.records}
    assert set(by_tool) == {"task", "secret_lookup"}
    # Section 6: `parent_run_id` is subagent lineage. The delegated row names
    # the run that delegated it; the parent's own row does not.
    assert by_tool["task"].run_id == run_context.run_id
    assert by_tool["task"].parent_run_id is None
    assert by_tool["secret_lookup"].parent_run_id == run_context.run_id
    assert by_tool["secret_lookup"].run_id != run_context.run_id
    assert by_tool["secret_lookup"].agent_principal == "agent://local/pilot"
    assert by_tool["secret_lookup"].harness_version == HARNESS_VERSION


def test_build_agent_refuses_middleware_without_evidence(backend) -> None:
    """Structural, not conventional: you cannot get an agent out of this
    function that is incapable of producing an audit trail."""
    model, _ = build_model("fake")

    with pytest.raises(MissingEvidenceMiddleware, match="non-negotiable"):
        build_agent(
            model=model,
            backend=backend,
            middleware=[ModelCallLimitMiddleware(run_limit=3, exit_behavior="end")],
        )


def test_build_agent_refuses_an_empty_middleware_stack(backend) -> None:
    model, _ = build_model("fake")
    with pytest.raises(MissingEvidenceMiddleware, match=r"\(none\)"):
        build_agent(model=model, backend=backend, middleware=[])


def test_build_agent_refuses_when_it_cannot_build_the_default_stack(backend) -> None:
    model, _ = build_model("fake")
    with pytest.raises(MissingEvidenceMiddleware, match="sink="):
        build_agent(model=model, backend=backend)


def test_a_custom_stack_containing_evidence_is_accepted(backend, run_context) -> None:
    sink = InMemorySink()
    model = ScriptedChatModel.from_script(final="done")
    agent = build_agent(
        model=model,
        backend=backend,
        middleware=[
            ToolErrorMiddleware(lambda exc, _req: f"failed: {type(exc).__name__}"),
            EvidenceMiddleware(sink, run_context),
        ],
    )
    assert agent.invoke({"messages": [{"role": "user", "content": "hi"}]})["messages"][-1].content


def test_load_prompt_is_read_from_a_file_and_is_honest_about_the_platform() -> None:
    """A file so it is reviewable and diffable; not a control, per P2."""
    prompt = load_prompt()
    assert "gateway" in prompt.lower()
    assert "recorded" in prompt.lower()
    assert "/skills/" in prompt


def test_load_prompt_accepts_an_override(tmp_path: Path) -> None:
    override = tmp_path / "system.md"
    override.write_text("You are a narrower agent.\n")
    assert load_prompt(override) == "You are a narrower agent.\n"


def test_default_permissions_are_restrictive_and_ordered() -> None:
    """First matching rule wins and `deepagents` falls through to allow, so
    the catch-all write deny has to be last or it means nothing."""
    from deepagents.middleware.filesystem import _check_fs_permission

    rules = default_permissions()
    assert _check_fs_permission(rules, "read", "/skills/web-research/SKILL.md") == "allow"
    assert _check_fs_permission(rules, "write", "/skills/web-research/SKILL.md") == "deny"
    assert _check_fs_permission(rules, "read", "/memories/notes.md") == "deny"
    assert _check_fs_permission(rules, "write", "/tmp/scratch.txt") == "allow"
    assert _check_fs_permission(rules, "write", "/workspace/out.json") == "allow"
    assert _check_fs_permission(rules, "write", "/etc/passwd") == "deny"


def test_build_model_is_explicit_about_provider_and_id() -> None:
    """`model_id` lands in every evidence row, so it names the provider too."""
    model, model_id = build_model("fake")
    assert isinstance(model, ScriptedChatModel)
    assert model_id == "fake:scripted"

    with pytest.raises(ValueError, match="model_name is required"):
        build_model("anthropic")

    with pytest.raises(ValueError, match="unknown model provider"):
        build_model("bedrock")  # type: ignore[arg-type]


def test_vertex_fails_with_a_clear_error_rather_than_an_import_traceback() -> None:
    """`langchain-google-vertexai` is not installed; nothing may need it to import."""
    from agent_platform.harness.models import UnavailableModelProvider

    with pytest.raises(UnavailableModelProvider, match=r"agent-platform\[gcp\]"):
        build_model("vertex", model_name="gemini-x")
