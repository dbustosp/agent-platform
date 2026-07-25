"""Claude Code as a second harness — §7.2's portability test, as tests.

The claim being checked is P1: the control plane is harness-agnostic. If it is,
a second harness needs no schema change to produce the same evidence. These
tests assert that literally — the rows a Claude Code session produces satisfy
the same `EvidenceRecord` schema, land in the same sink, and differ only in
`harness_version`.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from agent_platform.claude_code.hook import (
    CLAUDE_CODE_PRINCIPAL,
    read_intent,
    record_from_payload,
    run_hook,
    should_record,
)
from agent_platform.evidence.record import EvidenceRecord, PolicyDecision

REPO = Path(__file__).resolve().parents[1]

BASE_ENV = {
    "AGENT_PRINCIPAL": "agent://claude-code/test",
    "DELEGATED_BY": "tester",
}


def _payload(**overrides) -> dict:
    payload = {
        "session_id": "sess-abc",
        "transcript_path": "",
        "cwd": "/repo",
        "permission_mode": "default",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
        "tool_response": {"stdout": "3 passed", "exit_code": 0},
        "tool_use_id": "toolu_1",
    }
    payload.update(overrides)
    return payload


def _transcript(tmp_path: Path, *, intent: str, version: str = "2.1.215") -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "mode", "sessionId": "sess-abc"}),
                json.dumps(
                    {
                        "type": "user",
                        "version": version,
                        "sessionId": "sess-abc",
                        "message": {"role": "user", "content": intent},
                    }
                ),
                json.dumps({"type": "assistant", "message": {"role": "assistant"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


# ── the mapping ──────────────────────────────────────────────────────────────


def test_a_tool_call_becomes_a_section_6_record(tmp_path: Path):
    transcript = _transcript(tmp_path, intent="port the platform to Claude Code")
    record = record_from_payload(_payload(transcript_path=str(transcript)), env=dict(BASE_ENV))

    assert record is not None
    assert record.run_id == "sess-abc"
    assert record.parent_run_id is None
    assert record.agent_principal == "agent://claude-code/test"
    assert record.delegated_by == "tester"
    assert record.intent == "port the platform to Claude Code"
    assert record.tool == "Bash"
    assert record.policy_decision is PolicyDecision.ALLOW
    assert record.args_hash.startswith("sha256:")
    assert record.result_hash.startswith("sha256:")
    assert record.harness_version == "claude-code==2.1.215"

    # The payload is hashed, never stored: the command must not survive.
    serialised = json.dumps(record.to_dict())
    assert "pytest -q" not in serialised
    assert "3 passed" not in serialised
    # And it is the same schema the deepagents harness writes.
    assert EvidenceRecord.from_dict(json.loads(serialised)) == record


def test_a_subagent_call_records_lineage(tmp_path: Path):
    """`agent_id` present means a delegate; `session_id` is then the parent."""
    record = record_from_payload(
        _payload(agent_id="agent-xyz", agent_type="Explore"), env=dict(BASE_ENV)
    )
    assert record is not None
    assert record.run_id == "agent-xyz"
    assert record.parent_run_id == "sess-abc"


def test_run_id_is_stable_across_a_session():
    """Section 6's "stable across a task" is what `session_id` already means."""
    ids = {
        record_from_payload(_payload(tool_name=tool), env=dict(BASE_ENV)).run_id
        for tool in ("Bash", "Read", "Edit")
    }
    assert ids == {"sess-abc"}


def test_identical_calls_hash_identically_and_different_ones_do_not():
    a = record_from_payload(_payload(), env=dict(BASE_ENV))
    b = record_from_payload(_payload(), env=dict(BASE_ENV))
    c = record_from_payload(_payload(tool_input={"command": "ls"}), env=dict(BASE_ENV))
    assert a.args_hash == b.args_hash
    assert a.args_hash != c.args_hash


def test_a_missing_session_id_produces_no_row():
    """A row that cannot be correlated with anything is not evidence."""
    assert record_from_payload(_payload(session_id=""), env=dict(BASE_ENV)) is None


def test_the_principal_defaults_to_naming_claude_code():
    record = record_from_payload(_payload(), env={})
    assert record.agent_principal == CLAUDE_CODE_PRINCIPAL


# ── de-duplication against the gateway ───────────────────────────────────────


def test_gateway_tool_calls_are_left_to_the_gateway():
    """The gateway already wrote a row, with the real policy decision on it."""
    assert not should_record("mcp__agent-platform-gateway__recall", env={})
    assert not should_record("mcp__agent-platform-gateway__lookup_service_context", env={})
    assert should_record("Bash", env={})
    assert should_record("mcp__some-other-server__query", env={})

    skipped = record_from_payload(
        _payload(tool_name="mcp__agent-platform-gateway__recall"), env=dict(BASE_ENV)
    )
    assert skipped is None


def test_the_gateway_server_name_is_configurable():
    env = {**BASE_ENV, "GATEWAY_MCP_SERVER_NAME": "internal-gw"}
    assert not should_record("mcp__internal-gw__recall", env=env)
    assert should_record("mcp__agent-platform-gateway__recall", env=env)


# ── intent recovery ──────────────────────────────────────────────────────────


def test_intent_comes_from_the_first_user_message(tmp_path: Path):
    transcript = _transcript(tmp_path, intent="audit the checkout change")
    assert read_intent(transcript) == "audit the checkout change"


def test_intent_handles_block_style_content(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {}},
                        {"type": "text", "text": "what owns checkout?"},
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_intent(path) == "what owns checkout?"


def test_a_missing_or_broken_transcript_costs_the_intent_not_the_row(tmp_path: Path):
    assert read_intent(None) == ""
    assert read_intent(tmp_path / "nope.jsonl") == ""

    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n{also not\n", encoding="utf-8")
    assert read_intent(broken) == ""

    record = record_from_payload(_payload(transcript_path=str(broken)), env=dict(BASE_ENV))
    assert record is not None, "a bad transcript must not cost us the evidence row"
    assert record.intent.startswith("claude-code session")


# ── the hook must never break the session ────────────────────────────────────


def test_the_hook_writes_a_row_to_the_configured_sink(tmp_path: Path):
    env = {
        **BASE_ENV,
        "EVIDENCE_SINK": "jsonl",
        "EVIDENCE_PATH": str(tmp_path / "evidence.jsonl"),
        "EVIDENCE_SPILL_PATH": str(tmp_path / "spill.jsonl"),
    }
    code = run_hook(io.StringIO(json.dumps(_payload())), env=env)
    assert code == 0

    rows = [
        json.loads(line)
        for line in (tmp_path / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["tool"] == "Bash"
    assert rows[0]["run_id"] == "sess-abc"
    assert rows[0]["harness_version"].startswith("claude-code==")


def test_malformed_input_exits_cleanly(tmp_path: Path):
    env = {**BASE_ENV, "EVIDENCE_PATH": str(tmp_path / "e.jsonl")}
    for bad in ("", "   ", "not json", "[1,2,3]", "null"):
        assert run_hook(io.StringIO(bad), env=env) == 0
    assert not (tmp_path / "e.jsonl").exists()


def test_a_broken_sink_configuration_does_not_break_the_session(tmp_path: Path):
    """A hook that crashes the editor gets removed, which loses every row."""
    env = {
        **BASE_ENV,
        "EVIDENCE_SINK": "bigquery",  # no project set -> ConfigError inside
        "EVIDENCE_PATH": str(tmp_path / "e.jsonl"),
    }
    assert run_hook(io.StringIO(json.dumps(_payload())), env=env) == 0


def test_the_hook_runs_as_a_subprocess_the_way_claude_code_invokes_it(tmp_path: Path):
    """End to end through the real entrypoint, exactly as settings.json calls it."""
    proc = subprocess.run(
        [sys.executable, "-m", "agent_platform.claude_code.hook"],
        input=json.dumps(_payload(tool_name="Read", tool_input={"file_path": "/x"})),
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
        env={
            **BASE_ENV,
            "PATH": "/usr/bin:/bin",
            "EVIDENCE_SINK": "jsonl",
            "EVIDENCE_PATH": str(tmp_path / "evidence.jsonl"),
            "EVIDENCE_SPILL_PATH": str(tmp_path / "spill.jsonl"),
        },
    )
    assert proc.returncode == 0, proc.stderr
    rows = (tmp_path / "evidence.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["tool"] == "Read"


# ── the generated configuration ──────────────────────────────────────────────


def test_the_generated_config_matches_the_hook_and_server_it_describes():
    from agent_platform.cli import claude_code_hook_config, claude_code_mcp_config

    mcp = claude_code_mcp_config()
    server = mcp["mcpServers"]["agent-platform-gateway"]
    assert server["command"] == "agent-platform"
    assert server["args"] == ["gateway", "serve"]
    assert "/Users/" not in json.dumps(mcp), "committed config must not embed a home directory"
    # The server name must be the one the hook de-duplicates against.
    assert not should_record("mcp__agent-platform-gateway__recall", env={})

    hooks = claude_code_hook_config()["hooks"]["PostToolUse"]
    command = hooks[0]["hooks"][0]["command"]
    assert command == "agent-platform-hook"


def test_install_writes_config_and_links_skills_without_copying(tmp_path: Path):
    """Git stays the system of record (§3.3) — `.claude/skills` is a link."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_platform.cli",
            "claude-code",
            "install",
            "--dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
        env={**BASE_ENV, "PATH": "/usr/bin:/bin", "SKILLS_DIR": str(REPO / "skills")},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    mcp = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "agent-platform-gateway" in mcp["mcpServers"]

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["hooks"]["PostToolUse"]

    link = tmp_path / ".claude" / "skills"
    assert link.is_symlink(), "skills must be linked, not copied, so Git stays authoritative"
    assert (link / "service-change-review" / "SKILL.md").is_file()


def test_install_does_not_clobber_existing_config(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"mine": {}}}', encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_platform.cli",
            "claude-code",
            "install",
            "--dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
        env={**BASE_ENV, "PATH": "/usr/bin:/bin", "SKILLS_DIR": str(REPO / "skills")},
    )
    assert json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8")) == {
        "mcpServers": {"mine": {}}
    }


def test_the_hook_entrypoint_is_silent_on_stderr(tmp_path: Path):
    """Claude Code shows hook stderr to the user, on every single tool call.

    Running `-m agent_platform.claude_code.hook` triggers a CPython
    RuntimeWarning because the package already imported the submodule. The
    package entrypoint must not.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "agent_platform.claude_code"],
        input=json.dumps(_payload()),
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
        env={
            **BASE_ENV,
            "PATH": "/usr/bin:/bin",
            "EVIDENCE_PATH": str(tmp_path / "e.jsonl"),
            "EVIDENCE_SPILL_PATH": str(tmp_path / "s.jsonl"),
        },
    )
    assert proc.returncode == 0
    assert proc.stderr == "", f"hook wrote to stderr: {proc.stderr!r}"
    assert proc.stdout == "", f"hook wrote to stdout: {proc.stdout!r}"
    assert (tmp_path / "e.jsonl").is_file(), "and it still recorded the row"


def test_the_generated_hook_command_is_the_silent_entrypoint():
    from agent_platform.cli import claude_code_hook_config

    portable = claude_code_hook_config()["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert portable == "agent-platform-hook", "committed config must be path-independent"

    pinned = claude_code_hook_config("/opt/venv/bin/python")
    pinned_cmd = pinned["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert pinned_cmd.endswith("-m agent_platform.claude_code"), (
        "the pinned form must invoke the package, not the submodule"
    )
