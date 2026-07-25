"""End-to-end: config -> gateway -> harness -> evidence, with no credentials.

This is the test that says the pilot is real. Everything below runs the actual
composition root, the actual policy engine, the actual gateway, the actual
`create_deep_agent` assembly and the actual JSONL sink. The only thing faked is
the model, because Phase 1's exit criterion is "evidence rows accumulating" and
you cannot assert on rows a paid API happens to generate today.

The MCP-transport tests spawn a real gateway subprocess over stdio. They are the
only proof that §5's "one MCP connection to the gateway" works, and that the
tool surface an outside harness sees is read-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_platform.config import (
    Settings,
    build_gateway,
    build_run_context,
    build_sink,
    load_dotenv,
)
from agent_platform.evidence.record import EvidenceRecord, PolicyDecision
from agent_platform.harness import HARNESS_VERSION, build_agent, build_backend
from agent_platform.harness.gateway_tools import gateway_tools_in_process, stdio_connection
from agent_platform.harness.skills_store import GitSkillStore
from agent_platform.harness.testing import ScriptedChatModel

REPO = Path(__file__).resolve().parents[1]
KNOWLEDGE = REPO / "data" / "knowledge"
SKILLS = REPO / "skills"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        agent_principal="agent://test/e2e",
        delegated_by="tester",
        evidence_sink="jsonl",
        evidence_path=tmp_path / "evidence.jsonl",
        evidence_spill_path=tmp_path / "spill.jsonl",
        knowledge_dir=KNOWLEDGE,
        skills_dir=SKILLS,
    )


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_a_full_run_lands_evidence_on_disk(settings: Settings):
    """One scripted tool call produces one durable, well-formed evidence row."""
    sink = build_sink(settings)
    context = build_run_context(
        settings, "who owns checkout?", model_id="fake:scripted", harness_version=HARNESS_VERSION
    )
    gateway = build_gateway(settings, context, sink)

    model = ScriptedChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup_service_context",
                            "args": {"service": "checkout"},
                            "id": "call_1",
                        }
                    ],
                ),
                AIMessage(content="checkout-api is owned by payments-platform."),
            ]
        )
    )

    agent = build_agent(
        model=model,
        backend=build_backend(skill_store=GitSkillStore(settings.skills_dir)),
        tools=gateway_tools_in_process(gateway),
        sink=sink,
        run_context=context,
    )
    result = agent.invoke({"messages": [{"role": "user", "content": "who owns checkout?"}]})
    sink.close()

    assert "payments-platform" in result["messages"][-1].content

    rows = _rows(settings.evidence_path)
    assert len(rows) == 1, f"expected exactly one row, got {[r['tool'] for r in rows]}"
    row = rows[0]
    assert row["tool"] == "lookup_service_context"
    assert row["policy_decision"] == "allow"
    assert row["agent_principal"] == "agent://test/e2e"
    assert row["delegated_by"] == "tester"
    assert row["intent"] == "who owns checkout?"
    assert row["run_id"] == context.run_id
    assert row["harness_version"] == HARNESS_VERSION
    assert row["args_hash"].startswith("sha256:")
    assert row["result_hash"].startswith("sha256:")
    # Payloads are hashed, never stored (Section 6).
    assert "checkout" not in json.dumps({k: v for k, v in row.items() if k != "intent"})
    # And the row round-trips through the schema it claims to satisfy.
    assert EvidenceRecord.from_dict(row).policy_decision is PolicyDecision.ALLOW


def test_a_denied_tool_is_recorded_and_never_executes(settings: Settings, tmp_path: Path):
    """P2: enforcement is at the boundary, and a refusal is evidence."""
    locked = replace(settings, allowed_tools=frozenset({"recall"}))
    sink = build_sink(locked)
    context = build_run_context(locked, "probe")
    gateway = build_gateway(locked, context, sink)

    model = ScriptedChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup_service_context",
                            "args": {"service": "checkout"},
                            "id": "call_1",
                        }
                    ],
                ),
                AIMessage(content="I was refused."),
            ]
        )
    )
    agent = build_agent(
        model=model,
        backend=build_backend(skill_store=GitSkillStore(locked.skills_dir)),
        tools=gateway_tools_in_process(gateway),
        sink=sink,
        run_context=context,
    )
    agent.invoke({"messages": [{"role": "user", "content": "probe"}]})
    sink.close()

    rows = _rows(locked.evidence_path)
    assert len(rows) == 1
    assert rows[0]["policy_decision"] == "deny"
    assert rows[0]["tool"] == "lookup_service_context"
    # The refusal recorded no result: the tool body never ran.
    assert rows[0]["result_hash"] == "sha256:__empty__"


def test_settings_from_env_reads_the_documented_variables(monkeypatch, tmp_path: Path):
    """Every variable in .env.example must actually be read by something."""
    monkeypatch.setenv("AGENT_PRINCIPAL", "agent://ci/runner")
    monkeypatch.setenv("DELEGATED_BY", "ci")
    monkeypatch.setenv("EVIDENCE_SINK", "sqlite")
    monkeypatch.setenv("EVIDENCE_PATH", str(tmp_path / "e.db"))
    monkeypatch.setenv("EVIDENCE_SPILL_PATH", str(tmp_path / "s.jsonl"))
    monkeypatch.setenv("EVIDENCE_FAIL_CLOSED", "true")
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_MODEL_NAME", "claude-sonnet-4-6")
    monkeypatch.setenv("GATEWAY_ALLOWED_TOOLS", "recall")
    monkeypatch.setenv("AGENT_RUN_LIMIT", "7")

    s = Settings.from_env()
    assert s.agent_principal == "agent://ci/runner"
    assert s.delegated_by == "ci"
    assert s.evidence_sink == "sqlite"
    assert s.evidence_fail_closed is True
    assert s.model_provider == "anthropic"
    assert s.model_name == "claude-sonnet-4-6"
    assert s.allowed_tools == frozenset({"recall"})
    assert s.run_limit == 7


def test_bad_configuration_is_refused_not_guessed(monkeypatch):
    from agent_platform.config import ConfigError

    monkeypatch.setenv("EVIDENCE_SINK", "s3")
    with pytest.raises(ConfigError):
        Settings.from_env()

    monkeypatch.setenv("EVIDENCE_SINK", "jsonl")
    monkeypatch.setenv("AGENT_RUN_LIMIT", "0")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_dotenv_never_overrides_a_real_environment_variable(tmp_path: Path):
    env: dict[str, str] = {"AGENT_PRINCIPAL": "agent://real"}
    dotenv = tmp_path / ".env"
    dotenv.write_text("AGENT_PRINCIPAL=agent://from-file\nDELEGATED_BY=filey\n", encoding="utf-8")

    load_dotenv(dotenv, env=env)

    assert env["AGENT_PRINCIPAL"] == "agent://real", "an exported value must win"
    assert env["DELEGATED_BY"] == "filey", "an unset value comes from the file"


# ── MCP transport: a real subprocess ─────────────────────────────────────────


def _mcp_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "EVIDENCE_PATH": str(tmp_path / "evidence.jsonl"),
        "EVIDENCE_SPILL_PATH": str(tmp_path / "spill.jsonl"),
        "GATEWAY_KNOWLEDGE_DIR": str(KNOWLEDGE),
        "AGENT_PRINCIPAL": "agent://test/mcp",
        "DELEGATED_BY": "tester",
    }


async def test_the_mcp_surface_is_read_only_and_logs_to_our_store(tmp_path: Path):
    """§5: one MCP connection, read-only, and the rows are ours.

    Spawns the gateway as a separate process over stdio — the same thing a
    foreign harness would do. Nothing in this test imports the gateway.
    """
    from agent_platform.harness.gateway_tools import agateway_tools_over_mcp

    conn = stdio_connection(cwd=str(REPO), env=_mcp_env(tmp_path))
    tools = await agateway_tools_over_mcp(conn)

    names = {t.name for t in tools}
    assert names == {"lookup_service_context", "get_control_requirements", "recall"}
    assert "record_evidence" not in names, "the write tool must not be advertised to a model"

    by = {t.name: t for t in tools}
    await by["lookup_service_context"].ainvoke({"service": "checkout"})
    await by["recall"].ainvoke({"subject": "checkout"})

    rows = _rows(tmp_path / "evidence.jsonl")
    assert [r["tool"] for r in rows] == ["lookup_service_context", "recall"]
    assert {r["delegated_by"] for r in rows} == {"tester"}
    assert {r["agent_principal"] for r in rows} == {"agent://test/mcp"}
    assert all(r["policy_decision"] == "allow" for r in rows)


def test_the_gateway_module_refuses_to_run_without_a_chosen_sink():
    """The server must not pick an evidence destination for you."""
    proc = subprocess.run(
        [sys.executable, "-m", "agent_platform.gateway.server"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0
    assert "evidence sink" in (proc.stdout + proc.stderr)


def test_the_cli_exposes_the_phase_1_commands():
    proc = subprocess.run(
        [sys.executable, "-m", "agent_platform.cli", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    for command in ("run", "gateway", "evidence", "doctor"):
        assert command in proc.stdout


def test_doctor_reports_missing_optional_extras_without_crashing():
    """`google.cloud.bigquery` is absent here; doctor must say so, not raise."""
    proc = subprocess.run(
        [sys.executable, "-m", "agent_platform.cli", "doctor"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "missing" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_run_refuses_the_fake_provider_rather_than_pretending(tmp_path: Path):
    """`fake` replays a script. Letting it serve `run` would fake real work."""
    proc = subprocess.run(
        [sys.executable, "-m", "agent_platform.cli", "run", "do a thing"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
        env={
            **os.environ,
            "AGENT_MODEL_PROVIDER": "fake",
            "EVIDENCE_PATH": str(tmp_path / "e.jsonl"),
        },
    )
    assert proc.returncode == 2
    assert "cannot do real work" in proc.stderr


def test_build_agent_refuses_an_ambiguous_evidence_route(settings: Settings):
    """Passing both middleware= and sink= used to drop the sink on the floor."""
    from agent_platform.harness import EvidenceMiddleware, MissingEvidenceMiddleware

    sink = build_sink(settings)
    context = build_run_context(settings, "ambiguous")
    backend = build_backend(skill_store=GitSkillStore(settings.skills_dir))
    model = ScriptedChatModel(messages=iter([AIMessage(content="hi")]))

    with pytest.raises(MissingEvidenceMiddleware, match="silently ignored"):
        build_agent(
            model=model,
            backend=backend,
            sink=sink,
            run_context=context,
            middleware=[EvidenceMiddleware(sink=sink, run_context=context)],
        )
    sink.close()


async def test_the_agent_runs_over_mcp_and_evidence_lands_in_both_processes(tmp_path: Path):
    """The exact path `agent-platform run --mcp` takes, minus the paid model.

    Two processes write evidence for one task: the gateway subprocess records
    its own tool call (it holds the policy decision), and the harness records
    the calls the gateway never sees. Together they are the run.
    """
    from agent_platform.harness.gateway_tools import agateway_tools_over_mcp

    harness_evidence = tmp_path / "harness.jsonl"
    gateway_evidence = tmp_path / "gateway.jsonl"

    settings = Settings(
        agent_principal="agent://test/mcp-run",
        delegated_by="tester",
        evidence_path=harness_evidence,
        evidence_spill_path=tmp_path / "spill.jsonl",
        knowledge_dir=KNOWLEDGE,
        skills_dir=SKILLS,
    )
    # The gateway subprocess resolves identity from its own environment — MCP
    # stdio carries no principal — so the launching process has to pass one in.
    # Pinned by test_mcp_identity_comes_from_the_server_environment below.
    env = {
        **_mcp_env(tmp_path),
        "EVIDENCE_PATH": str(gateway_evidence),
        "AGENT_PRINCIPAL": settings.agent_principal,
        "DELEGATED_BY": settings.delegated_by,
    }
    tools = await agateway_tools_over_mcp(stdio_connection(cwd=str(REPO), env=env))

    sink = build_sink(settings)
    context = build_run_context(
        settings, "who owns checkout?", model_id="fake:scripted", harness_version=HARNESS_VERSION
    )
    model = ScriptedChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup_service_context",
                            "args": {"service": "checkout"},
                            "id": "call_1",
                        }
                    ],
                ),
                AIMessage(content="payments-platform owns it."),
            ]
        )
    )
    agent = build_agent(
        model=model,
        backend=build_backend(skill_store=GitSkillStore(settings.skills_dir)),
        tools=tools,
        sink=sink,
        run_context=context,
    )
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "who owns checkout?"}]})
    sink.close()

    assert "payments-platform" in result["messages"][-1].content

    gateway_rows = _rows(gateway_evidence)
    assert [r["tool"] for r in gateway_rows] == ["lookup_service_context"]
    assert gateway_rows[0]["policy_decision"] == "allow"
    assert gateway_rows[0]["agent_principal"] == "agent://test/mcp-run"

    # No duplicate: the harness skips gateway-owned tool names.
    harness_rows = _rows(harness_evidence)
    assert "lookup_service_context" not in [r["tool"] for r in harness_rows], (
        "a gateway tool must be recorded once, by the gateway"
    )


async def test_mcp_identity_comes_from_the_server_environment(tmp_path: Path):
    """A Phase-1 limitation, pinned so it is a known gap rather than a surprise.

    MCP stdio carries no caller identity. The gateway therefore records the
    principal configured in *its own* process, not one asserted by the client.
    In practice the launching harness passes its environment through, so they
    agree — but a gateway started by someone else records that someone else.

    Section 7.4 ("agent identity as first-class") and Phase 4's machine identity
    with short-lived credentials are what close this. Until then the mitigation
    is that the gateway is launched by the process that uses it.
    """
    from agent_platform.harness.gateway_tools import agateway_tools_over_mcp

    env = {
        **_mcp_env(tmp_path),
        "AGENT_PRINCIPAL": "agent://whoever-started-the-server",
        "DELEGATED_BY": "server-env",
    }
    tools = await agateway_tools_over_mcp(stdio_connection(cwd=str(REPO), env=env))
    by = {t.name: t for t in tools}
    await by["recall"].ainvoke({"subject": "checkout"})

    rows = _rows(tmp_path / "evidence.jsonl")
    assert rows, "the call must be recorded even though identity is server-side"
    assert rows[0]["agent_principal"] == "agent://whoever-started-the-server"
    assert rows[0]["delegated_by"] == "server-env"


async def test_run_id_is_stable_across_a_task_over_mcp(tmp_path: Path):
    """Section 6: `run_id  stable across a task`.

    A sessionless MCP tool opens a fresh session — and therefore spawns a fresh
    gateway process — for *every* call. Each process used to mint its own run
    id, so a three-call task produced three unrelated runs and the evidence log
    could not group them. The launching process now passes its identity down.
    """
    from agent_platform.config import run_context_env
    from agent_platform.harness.gateway_tools import agateway_tools_over_mcp

    settings = Settings(
        agent_principal="agent://test/stable",
        delegated_by="tester",
        evidence_path=tmp_path / "evidence.jsonl",
        evidence_spill_path=tmp_path / "spill.jsonl",
        knowledge_dir=KNOWLEDGE,
    )
    context = build_run_context(settings, "audit the checkout change")

    env = run_context_env(context, base=_mcp_env(tmp_path))
    tools = await agateway_tools_over_mcp(stdio_connection(cwd=str(REPO), env=env))
    by = {t.name: t for t in tools}

    await by["lookup_service_context"].ainvoke({"service": "checkout"})
    await by["recall"].ainvoke({"subject": "checkout"})
    await by["get_control_requirements"].ainvoke({"change_class": "schema migration"})

    rows = _rows(tmp_path / "evidence.jsonl")
    assert len(rows) == 3, "three calls, three rows"
    assert {r["run_id"] for r in rows} == {context.run_id}, (
        f"one task must be one run; got {sorted({r['run_id'] for r in rows})}"
    )
    # The intent recorded is the task, not the server's placeholder.
    assert {r["intent"] for r in rows} == {"audit the checkout change"}
    assert {r["agent_principal"] for r in rows} == {"agent://test/stable"}


def test_run_context_env_can_mark_a_child_run(tmp_path: Path):
    """`as_child=True` gives a delegated process its own id under this parent."""
    from agent_platform.config import run_context_env

    settings = Settings(agent_principal="agent://test/p", delegated_by="tester")
    parent = build_run_context(settings, "parent task")

    same = run_context_env(parent, base={})
    assert same["AGENT_RUN_ID"] == parent.run_id
    assert "AGENT_PARENT_RUN_ID" not in same

    child = run_context_env(parent, base={}, as_child=True)
    assert child["AGENT_RUN_ID"] != parent.run_id
    assert child["AGENT_PARENT_RUN_ID"] == parent.run_id
    assert child["AGENT_INTENT"] == "parent task"


def test_an_inherited_run_id_is_adopted_not_replaced():
    """The env contract `run_context_env` writes must be the one Settings reads."""
    settings = Settings.from_env(
        {
            "AGENT_RUN_ID": "run_inherited",
            "AGENT_PARENT_RUN_ID": "run_parent",
            "AGENT_INTENT": "the real task",
            "AGENT_PRINCIPAL": "agent://inherited",
            "DELEGATED_BY": "tester",
        }
    )
    context = build_run_context(settings, "a placeholder the CLI passed")
    assert context.run_id == "run_inherited"
    assert context.parent_run_id == "run_parent"
    assert context.intent == "the real task", "the task wins over the placeholder"


def test_harness_version_travels_with_the_run_identity():
    """The gateway cannot know which harness called it; the caller tells it."""
    from agent_platform.config import run_context_env

    settings = Settings(agent_principal="agent://x", delegated_by="t")
    context = build_run_context(settings, "task", harness_version="deepagents==0.6.12")

    env = run_context_env(context, base={})
    assert env["AGENT_HARNESS_VERSION"] == "deepagents==0.6.12"

    inherited = build_run_context(Settings.from_env(env), "placeholder")
    assert inherited.harness_version == "deepagents==0.6.12"
    assert inherited.run_id == context.run_id


def test_no_harness_version_is_recorded_as_null_not_invented():
    from agent_platform.config import run_context_env

    settings = Settings(agent_principal="agent://x", delegated_by="t")
    env = run_context_env(build_run_context(settings, "task"), base={})
    assert "AGENT_HARNESS_VERSION" not in env
    assert build_run_context(Settings.from_env(env), "t").harness_version is None
