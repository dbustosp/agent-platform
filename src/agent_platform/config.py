"""Composition root — turns environment into wired control-plane objects.

Every other module takes its collaborators as constructor arguments and reads no
configuration of its own. That is what lets the gateway be tested without a
sink, the sink without a gateway, and any of them without the harness (P5).
This module is the single place where those choices are actually made.

Import direction is one-way and load-bearing: `config` may import `evidence`,
`identity`, `policy`, and `gateway`. None of them may import `config`, and this
module may not import `agent_platform.harness` — the gateway has to stay
deployable on a machine where `deepagents` is not installed. Harness wiring
lives in the CLI, behind a lazy import.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agent_platform.evidence.record import new_run_id
from agent_platform.evidence.sink import EvidenceSink, SpillingSink
from agent_platform.gateway import READ_TOOL_NAMES, Gateway, YamlKnowledgeSource
from agent_platform.gateway.knowledge import default_knowledge_dir
from agent_platform.identity import Principal, RunContext
from agent_platform.policy import AllowlistPolicy, PolicyEngine

SinkKind = Literal["jsonl", "sqlite", "bigquery", "memory"]
ModelProvider = Literal["fake", "anthropic", "vertex"]

_TRUE = {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    """Raised when the environment describes a configuration we cannot build."""


def load_dotenv(path: str | Path = ".env", env: dict[str, str] | None = None) -> dict[str, str]:
    """Read a `.env` file into a dict without taking a dependency to do it.

    Existing environment variables win — an explicitly exported value should
    never be silently overridden by a stale file in the working directory.
    Missing file is not an error; local-first means the defaults already work.
    """
    target = os.environ if env is None else env
    p = Path(path)
    if not p.is_file():
        return {}
    loaded: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        target.setdefault(key, value)
    return loaded


@dataclass(frozen=True, slots=True)
class Settings:
    """The whole configurable surface of the platform, in one object."""

    agent_principal: str = "agent://local/pilot"
    delegated_by: str = "unknown"

    evidence_sink: SinkKind = "jsonl"
    evidence_path: Path = Path("./evidence/evidence.jsonl")
    evidence_spill_path: Path = Path("./evidence/spill.jsonl")
    evidence_fail_closed: bool = False

    bigquery_project: str | None = None
    bigquery_dataset: str = "agent_evidence_dev"
    bigquery_table: str = "tool_calls"

    model_provider: ModelProvider = "fake"
    model_name: str | None = None

    knowledge_dir: Path | None = None
    allowed_tools: frozenset[str] = field(default_factory=lambda: frozenset(READ_TOOL_NAMES))

    skills_dir: Path = Path("./skills")
    run_limit: int = 25

    # Inherited run identity. A gateway launched as a subprocess is serving
    # somebody else's task, and §6 says `run_id` is "stable across a task" —
    # so the launching process passes its identity down rather than letting
    # each child mint its own. Unset means "I am the top-level run".
    run_id: str | None = None
    parent_run_id: str | None = None
    intent: str | None = None
    # Which harness's call this process is serving. The gateway cannot know it
    # — the caller does — so it travels with the run identity or stays null.
    harness_version: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from the environment, falling back to local-first defaults."""
        e = os.environ if env is None else env

        def _path(key: str, default: Path) -> Path:
            raw = e.get(key)
            return Path(raw).expanduser() if raw else default

        sink = (e.get("EVIDENCE_SINK") or "jsonl").strip().lower()
        if sink not in {"jsonl", "sqlite", "bigquery", "memory"}:
            msg = f"EVIDENCE_SINK={sink!r} is not one of jsonl, sqlite, bigquery, memory"
            raise ConfigError(msg)

        provider = (e.get("AGENT_MODEL_PROVIDER") or "fake").strip().lower()
        if provider not in {"fake", "anthropic", "vertex"}:
            msg = f"AGENT_MODEL_PROVIDER={provider!r} is not one of fake, anthropic, vertex"
            raise ConfigError(msg)

        tools_raw = e.get("GATEWAY_ALLOWED_TOOLS")
        if tools_raw:
            allowed = frozenset(t.strip() for t in tools_raw.split(",") if t.strip())
        else:
            allowed = frozenset(READ_TOOL_NAMES)

        run_limit_raw = e.get("AGENT_RUN_LIMIT") or "25"
        try:
            run_limit = int(run_limit_raw)
        except ValueError as exc:
            msg = f"AGENT_RUN_LIMIT={run_limit_raw!r} is not an integer"
            raise ConfigError(msg) from exc
        if run_limit < 1:
            msg = f"AGENT_RUN_LIMIT must be at least 1, got {run_limit}"
            raise ConfigError(msg)

        knowledge_raw = e.get("GATEWAY_KNOWLEDGE_DIR")

        return cls(
            agent_principal=e.get("AGENT_PRINCIPAL") or "agent://local/pilot",
            delegated_by=(e.get("DELEGATED_BY") or e.get("USER") or e.get("USERNAME") or "unknown"),
            evidence_sink=sink,  # ty: ignore[invalid-argument-type]
            evidence_path=_path("EVIDENCE_PATH", Path("./evidence/evidence.jsonl")),
            evidence_spill_path=_path("EVIDENCE_SPILL_PATH", Path("./evidence/spill.jsonl")),
            evidence_fail_closed=(e.get("EVIDENCE_FAIL_CLOSED") or "").lower() in _TRUE,
            bigquery_project=e.get("BIGQUERY_PROJECT") or None,
            bigquery_dataset=e.get("BIGQUERY_DATASET") or "agent_evidence_dev",
            bigquery_table=e.get("BIGQUERY_TABLE") or "tool_calls",
            model_provider=provider,  # ty: ignore[invalid-argument-type]
            model_name=e.get("AGENT_MODEL_NAME") or None,
            knowledge_dir=Path(knowledge_raw).expanduser() if knowledge_raw else None,
            allowed_tools=allowed,
            skills_dir=_path("SKILLS_DIR", Path("./skills")),
            run_limit=run_limit,
            run_id=e.get("AGENT_RUN_ID") or None,
            parent_run_id=e.get("AGENT_PARENT_RUN_ID") or None,
            intent=e.get("AGENT_INTENT") or None,
            harness_version=e.get("AGENT_HARNESS_VERSION") or None,
        )

    def principal(self) -> Principal:
        return Principal(agent_principal=self.agent_principal, delegated_by=self.delegated_by)


def build_primary_sink(settings: Settings) -> EvidenceSink:
    """Construct the configured sink without the durability wrapper."""
    if settings.evidence_sink == "memory":
        from agent_platform.evidence.sinks.memory import InMemorySink

        return InMemorySink()
    if settings.evidence_sink == "jsonl":
        from agent_platform.evidence.sinks.jsonl import JsonlSink

        return JsonlSink(settings.evidence_path)
    if settings.evidence_sink == "sqlite":
        from agent_platform.evidence.sinks.sqlite import SqliteSink

        return SqliteSink(settings.evidence_path)
    if settings.evidence_sink == "bigquery":
        from agent_platform.evidence.sinks.bigquery import BigQuerySink

        if not settings.bigquery_project:
            msg = "EVIDENCE_SINK=bigquery requires BIGQUERY_PROJECT to be set"
            raise ConfigError(msg)
        return BigQuerySink(
            settings.bigquery_dataset,
            settings.bigquery_table,
            project=settings.bigquery_project,
        )
    msg = f"unhandled sink kind {settings.evidence_sink!r}"  # pragma: no cover
    raise ConfigError(msg)


def build_sink(settings: Settings) -> EvidenceSink:
    """The sink the platform actually uses: configured primary, wrapped for durability.

    Nothing in the system writes evidence through an unwrapped sink. A primary
    outage should cost us a replay job, not a hole in the record.
    """
    return SpillingSink(
        build_primary_sink(settings),
        settings.evidence_spill_path,
        fail_closed=settings.evidence_fail_closed,
    )


def build_policy(settings: Settings) -> PolicyEngine:
    """Default-deny allowlist over exactly the configured tools.

    `record_evidence` is not in the default allowlist and has to be added by
    hand. Phase 1's gateway connection is read-only.
    """
    return AllowlistPolicy(allowed_tools=frozenset(settings.allowed_tools))


def build_knowledge(settings: Settings) -> YamlKnowledgeSource:
    directory = settings.knowledge_dir or default_knowledge_dir()
    return YamlKnowledgeSource.from_directory(directory)


def build_run_context(
    settings: Settings,
    intent: str,
    *,
    model_id: str | None = None,
    harness_version: str | None = None,
) -> RunContext:
    """Start a run, or continue the one this process was handed.

    A gateway spawned over MCP is not starting a task — it is serving one. If
    the launching process passed its run identity down (see `run_context_env`),
    we adopt it, so every row for one task shares a `run_id` and an `intent`
    exactly as Section 6 requires. Without this, each stdio session mints a
    fresh id and the evidence log cannot group a task's calls at all.
    """
    return RunContext(
        principal=settings.principal(),
        intent=settings.intent or intent,
        run_id=settings.run_id or new_run_id(),
        parent_run_id=settings.parent_run_id,
        model_id=model_id,
        harness_version=harness_version or settings.harness_version,
    )


def run_context_env(
    context: RunContext,
    *,
    base: Mapping[str, str] | None = None,
    as_child: bool = False,
) -> dict[str, str]:
    """The environment a child process needs to keep writing *this* run's evidence.

    Used for the MCP gateway subprocess and for any other harness we hand work
    to. `as_child=True` records the child under a new run id whose parent is
    this one — the shape Section 6's `parent_run_id` exists for.
    """
    env = dict(os.environ if base is None else base)
    child = context.child() if as_child else context
    env.update(
        {
            "AGENT_PRINCIPAL": child.principal.agent_principal,
            "DELEGATED_BY": child.principal.delegated_by,
            "AGENT_RUN_ID": child.run_id,
            "AGENT_INTENT": child.intent,
        }
    )
    if child.parent_run_id:
        env["AGENT_PARENT_RUN_ID"] = child.parent_run_id
    else:
        env.pop("AGENT_PARENT_RUN_ID", None)
    if child.harness_version:
        env["AGENT_HARNESS_VERSION"] = child.harness_version
    else:
        env.pop("AGENT_HARNESS_VERSION", None)
    return env


def build_gateway(settings: Settings, context: RunContext, sink: EvidenceSink) -> Gateway:
    """Assemble the gateway from configuration.

    The sink is passed in rather than built here so that a single run shares one
    sink between the gateway and the harness middleware — two sinks over the
    same JSONL file would interleave writes from two file handles.
    """
    return Gateway(
        knowledge=build_knowledge(settings),
        policy=build_policy(settings),
        sink=sink,
        context=context,
    )
