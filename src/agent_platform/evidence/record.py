"""The evidence record — Section 6 of the plan.

This module is deliberately dependency-free. The record outlives the harness
that produced it, the sink that stored it, and very likely the framework this
repository was written against. Nothing here may import `deepagents`,
`langchain`, or any cloud SDK.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

SCHEMA_VERSION = "1"


class PolicyDecision(StrEnum):
    """Outcome of the policy check at the tool boundary (P2).

    `StrEnum` rather than `(str, Enum)` so that stringifying a decision anywhere
    — a log line, an f-string, a row handed to a driver that does not call
    `.value` — yields `"allow"` and not `"PolicyDecision.ALLOW"`. An evidence
    column that renders as a Python repr is a column somebody has to clean up
    later.
    """

    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


def new_run_id() -> str:
    """Generate a run id, stable across a single task."""
    return f"run_{uuid.uuid4().hex}"


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp. Never use naive datetimes in evidence."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One row per tool call, emitted at the point of action.

    Field-for-field the schema in Section 6. Two properties are load-bearing
    and must survive any refactor:

    - `harness_version` is a *field, not a dependency*. We record which harness
      produced the row; we do not ask the harness what the row means.
    - Payloads are hashed, never stored. `args_hash` and `result_hash` let us
      prove two calls were identical without holding the data, which keeps a
      data-classification conversation out of Phase 1.
    """

    run_id: str
    agent_principal: str
    delegated_by: str
    intent: str
    tool: str
    args_hash: str
    policy_decision: PolicyDecision
    result_hash: str | None = None
    parent_run_id: str | None = None
    approval_ref: str | None = None
    model_id: str | None = None
    harness_version: str | None = None
    timestamp: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            msg = "EvidenceRecord.timestamp must be timezone-aware (UTC)"
            raise ValueError(msg)
        for required in ("run_id", "agent_principal", "delegated_by", "tool", "args_hash"):
            if not getattr(self, required):
                msg = f"EvidenceRecord.{required} must be a non-empty string"
                raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to plain JSON-compatible types.

        Column order follows Section 6 so a human reading a JSONL file sees the
        schema as written in the plan.
        """
        out: dict[str, Any] = {}
        for name in FIELD_ORDER:
            value = getattr(self, name)
            if isinstance(value, datetime):
                out[name] = value.isoformat()
            elif isinstance(value, Enum):
                out[name] = value.value
            else:
                out[name] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceRecord:
        """Rehydrate a record written by `to_dict`."""
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        if isinstance(kwargs.get("timestamp"), str):
            kwargs["timestamp"] = datetime.fromisoformat(kwargs["timestamp"])
        if isinstance(kwargs.get("policy_decision"), str):
            kwargs["policy_decision"] = PolicyDecision(kwargs["policy_decision"])
        return cls(**kwargs)


#: Column order for tabular sinks (BigQuery, SQLite). Section 6 order.
FIELD_ORDER: tuple[str, ...] = (
    "run_id",
    "parent_run_id",
    "agent_principal",
    "delegated_by",
    "intent",
    "tool",
    "args_hash",
    "policy_decision",
    "approval_ref",
    "result_hash",
    "model_id",
    "harness_version",
    "timestamp",
    "schema_version",
)
