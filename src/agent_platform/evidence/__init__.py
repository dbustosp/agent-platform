"""The evidence log — the one non-negotiable (Section 6).

> Most technical debt is refinanceable. This is not.

Everything here is dependency-free by construction. No `deepagents`, no
`langchain`, no cloud SDK at module scope. That is P1 made mechanical: the
record, the hashing, and the sink protocol are the parts of this repository we
expect to still be running after the harness beneath them has been replaced
twice, so they must not know the harness exists.

P3 governs how they are used: evidence is emitted at the point of action, not
reconstructed from traces. A tracing exporter is welcome to receive a copy in
Phase 3; it may never be the only writer, because the test is whether the audit
trail survives the observability vendor being removed tomorrow.

This module is the import surface for everyone else — `from
agent_platform.evidence import EvidenceRecord, JsonlSink` — so that callers do
not have to know which submodule a name lives in, and so that moving one later
is not a breaking change.
"""

from agent_platform.evidence.hashing import (
    ALGORITHM,
    EMPTY,
    hash_args,
    hash_payload,
    hash_result,
)
from agent_platform.evidence.record import (
    FIELD_ORDER,
    SCHEMA_VERSION,
    EvidenceRecord,
    PolicyDecision,
    new_run_id,
    utc_now,
)
from agent_platform.evidence.sink import EvidenceSink, EvidenceSinkError, SpillingSink
from agent_platform.evidence.sinks import (
    BIGQUERY_SCHEMA,
    BigQueryField,
    BigQueryInsertError,
    BigQuerySink,
    InMemorySink,
    JsonlSink,
    SqliteSink,
    bigquery_schema_fields,
    create_table_ddl,
    table_id,
)

__all__ = [
    "ALGORITHM",
    "BIGQUERY_SCHEMA",
    "EMPTY",
    "FIELD_ORDER",
    "SCHEMA_VERSION",
    "BigQueryField",
    "BigQueryInsertError",
    "BigQuerySink",
    "EvidenceRecord",
    "EvidenceSink",
    "EvidenceSinkError",
    "InMemorySink",
    "JsonlSink",
    "PolicyDecision",
    "SpillingSink",
    "SqliteSink",
    "bigquery_schema_fields",
    "create_table_ddl",
    "hash_args",
    "hash_payload",
    "hash_result",
    "new_run_id",
    "table_id",
    "utc_now",
]
