"""Concrete evidence sinks.

Four destinations, one protocol, one row shape. That is the point: Section 4.4
promises that moving from a laptop to the governed cloud dataset is "a
constructor argument, not a migration", and it can only be one if the sinks are
interchangeable at the seam. Swapping `JsonlSink` for `BigQuerySink` changes
where the evidence lands and nothing about what it says.

None of these may import the harness or the gateway. Evidence outlives both.
"""

from agent_platform.evidence.sinks.bigquery import (
    BIGQUERY_SCHEMA,
    BigQueryField,
    BigQueryInsertError,
    BigQuerySink,
    bigquery_schema_fields,
    create_table_ddl,
    table_id,
)
from agent_platform.evidence.sinks.jsonl import JsonlSink
from agent_platform.evidence.sinks.memory import InMemorySink
from agent_platform.evidence.sinks.sqlite import SqliteSink

__all__ = [
    "BIGQUERY_SCHEMA",
    "BigQueryField",
    "BigQueryInsertError",
    "BigQuerySink",
    "InMemorySink",
    "JsonlSink",
    "SqliteSink",
    "bigquery_schema_fields",
    "create_table_ddl",
    "table_id",
]
