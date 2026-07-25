"""PostToolUse hook — the Claude Code equivalent of `EvidenceMiddleware`.

Claude Code invokes this once per completed tool call, handing it a JSON payload
on stdin. We turn that payload into the same `EvidenceRecord` the deepagents
middleware produces, and write it to the same sink. A reader of the evidence log
cannot tell which harness produced a row except by looking at `harness_version`,
which is exactly the property Section 6 asks for.

**This hook must never break the user's session.** It runs inside somebody's
editor loop. Every failure path logs and exits 0; the durability guarantee comes
from `SpillingSink` underneath, not from crashing the caller.

Field mapping, and why each one is defensible:

| Section 6 | Claude Code | Note |
|---|---|---|
| `run_id` | `session_id`, or `agent_id` in a subagent | already "stable across a task" |
| `parent_run_id` | `session_id` when `agent_id` is present | subagent lineage, for free |
| `intent` | first user message in the transcript | "the task as stated", literally |
| `tool` | `tool_name` | |
| `args_hash` | hash of `tool_input` | payload never stored |
| `result_hash` | hash of `tool_response` | payload never stored |
| `policy_decision` | always `allow` | see below |
| `harness_version` | `claude-code==<version>` | a field, not a dependency |

`policy_decision` is always `allow`, and that is honest rather than convenient:
`PostToolUse` fires only for tools that already ran, so a call we can see is by
definition one the harness permitted. Denials issued by Claude Code's own
permission system never reach this hook. Gateway calls are the ones with a real
policy decision, and the gateway records those itself.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from agent_platform.evidence.hashing import hash_args, hash_result
from agent_platform.evidence.record import EvidenceRecord, PolicyDecision

logger = logging.getLogger(__name__)

#: Default identity for a Claude Code session. Overridden by AGENT_PRINCIPAL.
CLAUDE_CODE_PRINCIPAL = "agent://claude-code/local"

#: Environment variable naming the gateway's MCP server, so this hook knows
#: which tool calls the gateway has already recorded for itself.
SERVER_NAME_ENV = "GATEWAY_MCP_SERVER_NAME"
DEFAULT_SERVER_NAME = "agent-platform-gateway"

#: Tools whose calls say nothing about what the agent did to the outside world.
#: Recording them buries the signal without adding evidence.
UNINTERESTING_TOOLS = frozenset({"TodoWrite"})


def gateway_tool_prefix(env: dict[str, str] | None = None) -> str:
    """The `mcp__<server>__` prefix identifying gateway-owned tool calls."""
    e = os.environ if env is None else env
    return f"mcp__{e.get(SERVER_NAME_ENV) or DEFAULT_SERVER_NAME}__"


def should_record(tool_name: str, *, env: dict[str, str] | None = None) -> bool:
    """Is this a tool call *we* are responsible for recording?

    Gateway calls are excluded because the gateway process already wrote a row
    for them, with the authoritative policy decision attached. Recording them
    here too would double-count every governed call — the same de-duplication
    `EvidenceMiddleware` does with `GATEWAY_TOOL_NAMES`.
    """
    if not tool_name:
        return False
    if tool_name.startswith(gateway_tool_prefix(env)):
        return False
    return tool_name not in UNINTERESTING_TOOLS


def read_intent(transcript_path: str | Path | None, *, limit: int = 400) -> str:
    """Recover "the task as stated" from the session transcript.

    The hook payload carries no prompt, but it does carry the transcript path,
    and the first user message in it is the task. Best-effort by design: a
    changed transcript format costs us the intent string, not the row.
    """
    if not transcript_path:
        return ""
    path = Path(transcript_path)
    if not path.is_file():
        return ""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "user":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()[:limit]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = str(block.get("text", "")).strip()
                            if text:
                                return text[:limit]
    except OSError:
        return ""
    return ""


def _harness_version(payload: dict[str, Any], transcript_version: str | None) -> str:
    version = transcript_version or payload.get("version") or "unknown"
    return f"claude-code=={version}"


def _transcript_version(transcript_path: str | Path | None) -> str | None:
    """Claude Code stamps its version on transcript records."""
    if not transcript_path:
        return None
    path = Path(transcript_path)
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                version = entry.get("version")
                if version:
                    return str(version)
    except OSError:
        return None
    return None


def record_from_payload(
    payload: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
    intent: str | None = None,
) -> EvidenceRecord | None:
    """Build the evidence record for one Claude Code tool call.

    Returns `None` when the call is one the gateway already recorded.
    """
    e = dict(os.environ if env is None else env)
    tool_name = str(payload.get("tool_name") or "")
    if not should_record(tool_name, env=e):
        return None

    session_id = str(payload.get("session_id") or "").strip()
    agent_id = payload.get("agent_id")

    # In a subagent, `agent_id` identifies the delegate and `session_id` is the
    # parent session — which is precisely Section 6's lineage pair.
    if agent_id:
        run_id, parent_run_id = str(agent_id), session_id or None
    else:
        run_id, parent_run_id = session_id, None

    if not run_id:
        # Without a run id the row cannot be grouped with anything, and a row
        # that cannot be correlated is not evidence.
        return None

    transcript = payload.get("transcript_path")
    resolved_intent = intent if intent is not None else read_intent(transcript)

    return EvidenceRecord(
        run_id=run_id,
        parent_run_id=parent_run_id,
        agent_principal=e.get("AGENT_PRINCIPAL") or CLAUDE_CODE_PRINCIPAL,
        delegated_by=(e.get("DELEGATED_BY") or e.get("USER") or e.get("USERNAME") or "unknown"),
        intent=resolved_intent or f"claude-code session {run_id}",
        tool=tool_name,
        args_hash=hash_args(payload.get("tool_input")),
        # PostToolUse only fires for tools that ran; see the module docstring.
        policy_decision=PolicyDecision.ALLOW,
        result_hash=hash_result(payload.get("tool_response")),
        model_id=e.get("AGENT_MODEL_NAME") or None,
        harness_version=_harness_version(payload, _transcript_version(transcript)),
    )


def run_hook(stdin: Any = None, *, env: dict[str, str] | None = None) -> int:
    """Read one PostToolUse payload and write its evidence row.

    Always returns 0. A hook that fails loudly inside somebody's editor teaches
    them to remove the hook, which is a worse outcome for the evidence log than
    any single missing row.
    """
    stream = sys.stdin if stdin is None else stdin
    try:
        raw = stream.read()
    except Exception:  # noqa: BLE001
        logger.exception("evidence hook: could not read stdin")
        return 0

    if not raw or not raw.strip():
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("evidence hook: stdin was not JSON")
        return 0

    if not isinstance(payload, dict):
        return 0

    try:
        record = record_from_payload(payload, env=env)
        if record is None:
            return 0

        # Imported here so a broken sink config cannot stop the hook from
        # loading, and so `record_from_payload` stays unit-testable with no
        # filesystem involved.
        from agent_platform.config import Settings, build_sink, load_dotenv

        load_dotenv()
        sink = build_sink(Settings.from_env(env))
        try:
            sink.emit(record)
        finally:
            sink.close()
    except Exception:  # noqa: BLE001
        logger.exception("evidence hook: failed to record %s", payload.get("tool_name"))
    return 0


def main() -> int:  # pragma: no cover - entrypoint shim
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    return run_hook()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
