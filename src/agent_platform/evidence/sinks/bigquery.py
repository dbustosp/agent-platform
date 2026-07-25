"""BigQuery evidence sink — the Section 4.4 Phase 1 target.

Section 4.4 puts evidence in a BigQuery dev dataset in Phase 1 and the same
BigQuery under formal retention and access control in Phase 3. Deliberately a
constructor argument, not a migration: the row this sink writes is the row every
other sink in this package writes.

`google-cloud-bigquery` is an optional extra (`agent-platform[gcp]`) and is
imported lazily, inside the call that needs it. That is not politeness about
start-up time — it is the rule that nothing in this repository may require
credentials or a network to import, run, or test. The schema constants and the
DDL helper below therefore work with the SDK absent, which is what lets Phase 3
provision the governed dataset from a machine that has never installed it.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass, fields

from agent_platform.evidence.record import FIELD_ORDER, EvidenceRecord
from agent_platform.evidence.sink import EvidenceSinkError

DEFAULT_TABLE = "evidence"

_INSTALL_HINT = (
    "google-cloud-bigquery is not installed. Install the optional extra with "
    "`pip install 'agent-platform[gcp]'`, or use JsonlSink / SqliteSink for a "
    "local run — evidence must be written either way."
)

#: One-line meaning per column, carried into the table so the governed dataset
#: documents itself to whoever reads it in month four.
_DESCRIPTIONS: dict[str, str] = {
    "run_id": "Stable across a task.",
    "parent_run_id": "Subagent lineage; null for a top-level run.",
    "agent_principal": "The non-human identity acting.",
    "delegated_by": "The human on whose authority it acts.",
    "intent": "The task as stated.",
    "tool": "Tool name.",
    "args_hash": "Hash of the tool arguments, never the payload.",
    "policy_decision": "allow | deny | escalate.",
    "approval_ref": "HITL approval id, where applicable.",
    "result_hash": "Hash of the tool result, never the payload.",
    "model_id": "Model and version.",
    "harness_version": "The harness that produced the row. A field, not a dependency.",
    "timestamp": "When the action was taken (UTC).",
    "schema_version": "Version of this evidence schema.",
}

# Nullability is derived from the record type rather than restated, so the
# table cannot drift from `EvidenceRecord`. With `from __future__ import
# annotations` in record.py the annotation arrives as a string; `str(...)`
# normalises both cases.
_ANNOTATIONS: dict[str, str] = {f.name: str(f.type) for f in fields(EvidenceRecord)}


@dataclass(frozen=True, slots=True)
class BigQueryField:
    """A column definition that does not need the BigQuery SDK to exist."""

    name: str
    field_type: str
    mode: str
    description: str = ""


def _field_type(name: str) -> str:
    return "TIMESTAMP" if name == "timestamp" else "STRING"


def _field_mode(name: str) -> str:
    return "NULLABLE" if "None" in _ANNOTATIONS.get(name, "") else "REQUIRED"


#: The evidence table, in Section 6 column order.
BIGQUERY_SCHEMA: tuple[BigQueryField, ...] = tuple(
    BigQueryField(
        name=name,
        field_type=_field_type(name),
        mode=_field_mode(name),
        description=_DESCRIPTIONS.get(name, ""),
    )
    for name in FIELD_ORDER
)


def _import_bigquery():
    """Import `google.cloud.bigquery` or fail with something a human can act on.

    Returns the module. Untyped on purpose: annotating it would require the SDK
    at import time, which is the coupling this whole module exists to avoid.
    """
    try:
        return importlib.import_module("google.cloud.bigquery")
    except ImportError as exc:
        raise EvidenceSinkError(_INSTALL_HINT) from exc


def bigquery_schema_fields() -> list:
    """`BIGQUERY_SCHEMA` as `google.cloud.bigquery.SchemaField` objects.

    Requires the SDK; `BIGQUERY_SCHEMA` itself does not.
    """
    bigquery = _import_bigquery()
    return [
        bigquery.SchemaField(
            field.name,
            field.field_type,
            mode=field.mode,
            description=field.description or None,
        )
        for field in BIGQUERY_SCHEMA
    ]


def table_id(dataset: str, table: str = DEFAULT_TABLE, *, project: str | None = None) -> str:
    """Fully-qualified `project.dataset.table`, or `dataset.table` without one."""
    return f"{project}.{dataset}.{table}" if project else f"{dataset}.{table}"


def create_table_ddl(
    dataset: str,
    table: str = DEFAULT_TABLE,
    *,
    project: str | None = None,
    partition_expiration_days: int | None = None,
) -> str:
    """The `CREATE TABLE` statement for the evidence table.

    Emitted as text rather than executed so Phase 3 can put the governed dataset
    under the same review as any other schema change — open question 5 in the
    plan is retention and access model, and that is answered by a reviewed DDL,
    not by a client library call at start-up.

    Partitioned by day and clustered on `run_id` because both retention
    ("delete partitions older than N") and the only query anyone actually runs
    ("what did this run do?") are cheap that way.
    """
    columns = ",\n".join(
        f"  {f.name} {f.field_type}{'' if f.mode == 'NULLABLE' else ' NOT NULL'}"
        f"{_ddl_description(f)}"
        for f in BIGQUERY_SCHEMA
    )
    options = ['description = "Agent platform evidence log — one row per tool call (Section 6)"']
    if partition_expiration_days is not None:
        options.append(f"partition_expiration_days = {partition_expiration_days}")
    options_sql = ",\n  ".join(options)
    return (
        f"CREATE TABLE IF NOT EXISTS `{table_id(dataset, table, project=project)}` (\n"
        f"{columns}\n"
        f")\n"
        f"PARTITION BY DATE(timestamp)\n"
        f"CLUSTER BY run_id, tool\n"
        f"OPTIONS (\n  {options_sql}\n)"
    )


def _ddl_description(field: BigQueryField) -> str:
    if not field.description:
        return ""
    escaped = field.description.replace('"', '\\"')
    return f' OPTIONS(description = "{escaped}")'


class BigQueryInsertError(EvidenceSinkError):
    """A batch that BigQuery refused, with the records still attached.

    The rows are carried on the exception rather than dropped so the caller —
    in Phase 1, `SpillingSink` — can put the whole failed batch somewhere
    durable. An insert error must cost us a retry, never a row.
    """

    def __init__(self, message: str, records: tuple[EvidenceRecord, ...]) -> None:
        self.records = records
        super().__init__(f"{message} ({len(records)} record(s) affected)")


class BigQuerySink:
    """Stream evidence rows into a BigQuery table.

    `batch_size` defaults to 1: one row per tool call, written at the point of
    action, which is exactly what P3 asks for. Raise it only if streaming-insert
    volume becomes a cost problem, and understand what you are buying — every
    increment widens the window in which a crash loses rows that are, by
    Section 6, the one thing that cannot be backfilled.

    `client` exists to be injected. A fake with an `insert_rows_json(table, rows)`
    method is enough to test every path here, so the SDK is never required to
    prove the sink works.
    """

    def __init__(
        self,
        dataset: str,
        table: str = DEFAULT_TABLE,
        *,
        project: str | None = None,
        client: object | None = None,
        batch_size: int = 1,
    ) -> None:
        if batch_size < 1:
            msg = "batch_size must be at least 1"
            raise ValueError(msg)
        self.dataset = dataset
        self.table = table
        self.project = project
        self.batch_size = batch_size
        self.table_id = table_id(dataset, table, project=project)
        self._buffer: list[EvidenceRecord] = []
        self._buffer_lock = threading.Lock()
        # Sends are serialised separately from buffering so a slow insert does
        # not block every other thread from recording that it acted.
        self._send_lock = threading.Lock()
        self._closed = False
        self._client = client if client is not None else self._build_client()

    def _build_client(self) -> object:
        bigquery = _import_bigquery()
        return bigquery.Client(project=self.project)

    @property
    def pending(self) -> int:
        """Rows buffered but not yet sent. Non-zero only when batch_size > 1."""
        with self._buffer_lock:
            return len(self._buffer)

    def emit(self, record: EvidenceRecord) -> None:
        with self._buffer_lock:
            if self._closed:
                msg = f"BigQuerySink({self.table_id}) is closed; refusing to drop a record"
                raise EvidenceSinkError(msg)
            self._buffer.append(record)
            if len(self._buffer) < self.batch_size:
                return
            batch = tuple(self._buffer)
            self._buffer.clear()
        self._send(batch)

    def flush(self) -> None:
        """Send anything buffered. Safe to call on an empty buffer."""
        with self._buffer_lock:
            if not self._buffer:
                return
            batch = tuple(self._buffer)
            self._buffer.clear()
        self._send(batch)

    def _send(self, batch: tuple[EvidenceRecord, ...]) -> None:
        rows = [self.row(record) for record in batch]
        with self._send_lock:
            try:
                errors = self._client.insert_rows_json(self.table_id, rows)  # type: ignore[attr-defined]
            except Exception as exc:
                raise BigQueryInsertError(
                    f"BigQuery insert into {self.table_id} raised: {exc}", batch
                ) from exc
        if errors:
            raise BigQueryInsertError(
                f"BigQuery rejected rows for {self.table_id}: {errors}", batch
            )

    @staticmethod
    def row(record: EvidenceRecord) -> dict[str, object]:
        """One record as a BigQuery JSON row, keys in `FIELD_ORDER`.

        `to_dict` already renders the timestamp as RFC 3339 and the policy
        decision as its string value, which is what `insert_rows_json` wants.
        """
        payload = record.to_dict()
        return {name: payload.get(name) for name in FIELD_ORDER}

    def close(self) -> None:
        """Send anything still buffered, then refuse further emits.

        The closed flag is set and the buffer drained in the *same* critical
        section. Setting it afterwards leaves a window in which a concurrent
        `emit` returns successfully, appends to a buffer nothing will ever
        drain, and the row is never sent, never spilled, and never logged —
        silent evidence loss, which is the one failure Section 6 says cannot be
        recovered from. Closing first means a racing caller is told the sink is
        shut rather than quietly lied to.
        """
        with self._buffer_lock:
            if self._closed:
                return
            self._closed = True
            batch = tuple(self._buffer)
            self._buffer.clear()
        if batch:
            self._send(batch)
