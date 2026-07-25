"""Every sink must be interchangeable, append-only, and durable.

Section 4.4 promises that local-to-cloud is "a constructor argument, not a
migration". That is only true if a record written through one sink comes back
identical from any other, so the round-trip test is parametrised across all
four rather than written once per sink.

The rest of this file tests the properties the plan actually leans on:
append-only is enforced by the store rather than promised by the class, a
process that dies mid-run still leaves its last row on disk, and the BigQuery
path is provable with no SDK, no credentials, and no network.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_platform.evidence.record import (
    FIELD_ORDER,
    EvidenceRecord,
    PolicyDecision,
)
from agent_platform.evidence.sink import EvidenceSink, EvidenceSinkError
from agent_platform.evidence.sinks import (
    BIGQUERY_SCHEMA,
    BigQueryInsertError,
    BigQuerySink,
    InMemorySink,
    JsonlSink,
    SqliteSink,
    bigquery_schema_fields,
    create_table_ddl,
    table_id,
)
from agent_platform.evidence.sinks import bigquery as bigquery_module


def make_record(n: int = 0, **overrides) -> EvidenceRecord:
    base = {
        "run_id": f"run_{n:04d}",
        "agent_principal": "agent://local/pilot",
        "delegated_by": "tester",
        "intent": "look up who owns checkout-service",
        "tool": "lookup_service_context",
        "args_hash": f"sha256:{n:064x}",
        "policy_decision": PolicyDecision.ALLOW,
        "timestamp": datetime(2026, 7, 25, 12, 0, n % 60, tzinfo=UTC),
    }
    base.update(overrides)
    return EvidenceRecord(**base)


class FakeBigQueryClient:
    """Stands in for `google.cloud.bigquery.Client`.

    `insert_rows_json` is the whole surface `BigQuerySink` uses, which is what
    makes the BigQuery path testable without the SDK.
    """

    def __init__(self, *, errors: list | None = None, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, list[dict]]] = []
        self._errors = errors or []
        self._raises = raises

    def insert_rows_json(self, table: str, rows: list[dict]) -> list:
        self.calls.append((table, list(rows)))
        if self._raises is not None:
            raise self._raises
        return self._errors

    @property
    def rows(self) -> list[dict]:
        return [row for _, batch in self.calls for row in batch]


class BlockingBigQueryClient(FakeBigQueryClient):
    """A client that parks inside `insert_rows_json` until released.

    Lets a test drive the emit/close race deterministically instead of hoping
    a `sleep` lands on the right side of it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def insert_rows_json(self, table: str, rows: list[dict]) -> list:
        self.entered.set()
        self.release.wait(5)
        return super().insert_rows_json(table, rows)


class _NoBigQueryImportlib:
    """Stands in for `importlib` inside the bigquery module, with the SDK gone.

    Patched onto the module's own reference rather than onto `importlib`
    itself, so the rest of the interpreter can still import things while the
    test runs.
    """

    @staticmethod
    def import_module(name: str):
        msg = f"No module named {name!r}"
        raise ImportError(msg)


# --------------------------------------------------------------------------
# Interchangeability: one record shape, four destinations.
# --------------------------------------------------------------------------


@pytest.fixture(params=["memory", "jsonl", "sqlite", "bigquery"])
def sink_case(request: pytest.FixtureRequest, tmp_path: Path):
    """Yield `(sink, read_back)` for each sink, where `read_back()` returns records."""
    name = request.param
    if name == "memory":
        sink = InMemorySink()
        yield sink, lambda: list(sink.records)
    elif name == "jsonl":
        path = tmp_path / "logs" / "evidence.jsonl"
        sink = JsonlSink(path)
        yield sink, lambda: JsonlSink.read(path)
    elif name == "sqlite":
        sink = SqliteSink(tmp_path / "logs" / "evidence.db")
        yield sink, sink.read
    else:
        client = FakeBigQueryClient()
        sink = BigQuerySink("agent_platform_dev", client=client, batch_size=1)
        yield sink, lambda: [EvidenceRecord.from_dict(row) for row in client.rows]
    sink.close()


class TestEverySink:
    def test_satisfies_the_protocol(self, sink_case) -> None:
        sink, _ = sink_case
        assert isinstance(sink, EvidenceSink)

    def test_round_trips_a_minimal_record(self, sink_case) -> None:
        sink, read_back = sink_case
        record = make_record()
        sink.emit(record)
        assert read_back() == [record]

    def test_round_trips_a_fully_populated_record(self, sink_case) -> None:
        sink, read_back = sink_case
        record = make_record(
            parent_run_id="run_parent",
            approval_ref="approval-17",
            result_hash="sha256:cafe",
            model_id="vertex/gemini-x.y",
            harness_version="deepagents==0.6.12",
            policy_decision=PolicyDecision.ESCALATE,
        )
        sink.emit(record)
        assert read_back() == [record]

    def test_preserves_order_and_every_row(self, sink_case) -> None:
        sink, read_back = sink_case
        records = [make_record(n) for n in range(25)]
        for record in records:
            sink.emit(record)
        assert read_back() == records

    def test_close_is_idempotent(self, sink_case) -> None:
        sink, _ = sink_case
        sink.emit(make_record())
        sink.close()
        sink.close()
        sink.close()

    def test_emit_after_close_raises_rather_than_dropping(self, sink_case) -> None:
        sink, _ = sink_case
        sink.close()
        with pytest.raises(EvidenceSinkError):
            sink.emit(make_record())

    def test_concurrent_emits_lose_nothing(self, sink_case) -> None:
        sink, read_back = sink_case
        threads = 8
        per_thread = 20

        def worker(offset: int) -> None:
            for i in range(per_thread):
                sink.emit(make_record(offset * per_thread + i))

        workers = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        recovered = read_back()
        assert len(recovered) == threads * per_thread
        assert len({r.args_hash for r in recovered}) == threads * per_thread


# --------------------------------------------------------------------------
# JSONL
# --------------------------------------------------------------------------


class TestJsonlSink:
    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "c" / "evidence.jsonl"
        JsonlSink(path).close()
        assert path.exists()

    def test_one_json_object_per_line(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.jsonl"
        sink = JsonlSink(path)
        for n in range(3):
            sink.emit(make_record(n))
        sink.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        run_ids = [json.loads(line)["run_id"] for line in lines]
        assert run_ids == ["run_0000", "run_0001", "run_0002"]

    def test_is_append_only_across_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.jsonl"
        first = JsonlSink(path)
        first.emit(make_record(0))
        first.close()

        second = JsonlSink(path)
        second.emit(make_record(1))
        second.close()

        # A sink that truncated on open would leave one row here. The whole
        # point of the evidence log is that reopening continues history.
        assert [r.run_id for r in JsonlSink.read(path)] == ["run_0000", "run_0001"]

    def test_rows_are_readable_before_close(self, tmp_path: Path) -> None:
        # Flushed at emit, not at close: a long-running agent's evidence has to
        # be visible while it is still running.
        path = tmp_path / "evidence.jsonl"
        sink = JsonlSink(path)
        sink.emit(make_record(0))
        assert len(JsonlSink.read(path)) == 1
        sink.close()

    def test_fsync_off_still_flushes_every_row(self, tmp_path: Path) -> None:
        # Two separate guarantees share one constructor argument. fsync is
        # about surviving power loss; the per-emit flush is about the row being
        # readable at all. Turning the first off must not turn the second off.
        path = tmp_path / "evidence.jsonl"
        sink = JsonlSink(path, fsync=False)
        sink.emit(make_record(0))
        assert [r.run_id for r in JsonlSink.read(path)] == ["run_0000"]
        sink.emit(make_record(1))
        assert [r.run_id for r in JsonlSink.read(path)] == ["run_0000", "run_0001"]
        sink.close()
        assert [r.run_id for r in JsonlSink.read(path)] == ["run_0000", "run_0001"]

    def test_last_row_survives_a_process_that_never_closes(self, tmp_path: Path) -> None:
        # `os._exit` skips atexit hooks, destructors, and interpreter buffer
        # flushing — the closest thing to a crash we can arrange in a test.
        path = tmp_path / "evidence.jsonl"
        program = textwrap.dedent(
            f"""
            import os
            from datetime import UTC, datetime
            from agent_platform.evidence.record import EvidenceRecord, PolicyDecision
            from agent_platform.evidence.sinks.jsonl import JsonlSink

            sink = JsonlSink({str(path)!r})
            sink.emit(EvidenceRecord(
                run_id="run_crash",
                agent_principal="agent://local/pilot",
                delegated_by="tester",
                intent="die immediately after emitting",
                tool="lookup_service_context",
                args_hash="sha256:00",
                policy_decision=PolicyDecision.ALLOW,
                timestamp=datetime(2026, 7, 25, tzinfo=UTC),
            ))
            os._exit(0)
            """
        )
        subprocess.run([sys.executable, "-c", program], check=True)
        assert [r.run_id for r in JsonlSink.read(path)] == ["run_crash"]

    def test_read_of_a_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert JsonlSink.read(tmp_path / "never-written.jsonl") == []

    def test_read_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.jsonl"
        sink = JsonlSink(path)
        sink.emit(make_record(0))
        sink.close()
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n")
        assert len(JsonlSink.read(path)) == 1

    def test_a_torn_final_line_does_not_hide_the_rest(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.jsonl"
        sink = JsonlSink(path)
        sink.emit(make_record(0))
        sink.close()
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"run_id": "run_torn", "age')
        assert [r.run_id for r in JsonlSink.read(path)] == ["run_0000"]

    def test_strict_read_refuses_to_narrow_its_own_input(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.jsonl"
        sink = JsonlSink(path)
        sink.emit(make_record(0))
        sink.close()
        with path.open("a", encoding="utf-8") as fh:
            fh.write("not json\n")
        with pytest.raises(EvidenceSinkError, match="valid evidence record"):
            JsonlSink.read(path, strict=True)

    def test_tail_returns_the_most_recent_rows_oldest_first(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.jsonl"
        sink = JsonlSink(path)
        for n in range(10):
            sink.emit(make_record(n))
        sink.close()
        assert [r.run_id for r in JsonlSink.tail(path, 3)] == ["run_0007", "run_0008", "run_0009"]
        assert JsonlSink.tail(path, 0) == []
        assert len(JsonlSink.tail(path, 100)) == 10

    def test_non_ascii_survives_the_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.jsonl"
        sink = JsonlSink(path)
        record = make_record(intent="quién es el dueño de checkout-service ✅")
        sink.emit(record)
        sink.close()
        assert JsonlSink.read(path) == [record]


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------


class TestSqliteSink:
    def test_column_order_matches_field_order(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.db"
        SqliteSink(path).close()
        conn = sqlite3.connect(path)
        try:
            columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(evidence)"))
        finally:
            conn.close()
        assert columns == FIELD_ORDER

    def test_required_columns_are_not_null(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.db"
        SqliteSink(path).close()
        conn = sqlite3.connect(path)
        try:
            notnull = {row[1]: bool(row[3]) for row in conn.execute("PRAGMA table_info(evidence)")}
        finally:
            conn.close()
        assert notnull["run_id"] is True
        assert notnull["timestamp"] is True
        assert notnull["parent_run_id"] is False
        assert notnull["approval_ref"] is False

    def test_update_is_refused_by_the_database(self, tmp_path: Path) -> None:
        # Not "there is no update() method" — an actual trigger, so a person at
        # the sqlite3 prompt cannot rewrite history either.
        path = tmp_path / "evidence.db"
        sink = SqliteSink(path)
        sink.emit(make_record())
        sink.close()
        conn = sqlite3.connect(path)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("UPDATE evidence SET tool = 'tampered'")
        finally:
            conn.close()

    def test_delete_is_refused_by_the_database(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.db"
        sink = SqliteSink(path)
        sink.emit(make_record())
        sink.close()
        conn = sqlite3.connect(path)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM evidence")
        finally:
            conn.close()

    def test_reopening_continues_the_table(self, tmp_path: Path) -> None:
        path = tmp_path / "evidence.db"
        first = SqliteSink(path)
        first.emit(make_record(0))
        first.close()
        second = SqliteSink(path)
        second.emit(make_record(1))
        assert [r.run_id for r in second.read()] == ["run_0000", "run_0001"]
        second.close()

    def test_read_filters_by_run_id(self, tmp_path: Path) -> None:
        sink = SqliteSink(tmp_path / "evidence.db")
        sink.emit(make_record(0, run_id="run_a"))
        sink.emit(make_record(1, run_id="run_b"))
        sink.emit(make_record(2, run_id="run_a"))
        assert len(sink.read(run_id="run_a")) == 2
        sink.close()

    def test_read_limit_returns_the_most_recent_oldest_first(self, tmp_path: Path) -> None:
        sink = SqliteSink(tmp_path / "evidence.db")
        for n in range(10):
            sink.emit(make_record(n))
        assert [r.run_id for r in sink.read(limit=3)] == ["run_0007", "run_0008", "run_0009"]
        sink.close()

    def test_duplicate_records_are_both_kept(self, tmp_path: Path) -> None:
        # Two identical tool calls are two things that happened. A uniqueness
        # constraint here would silently under-report activity.
        sink = SqliteSink(tmp_path / "evidence.db")
        record = make_record()
        sink.emit(record)
        sink.emit(record)
        assert sink.read() == [record, record]
        sink.close()

    def test_read_after_close_raises_the_sinks_own_error(self, tmp_path: Path) -> None:
        # Not sqlite3.ProgrammingError: a caller holding an EvidenceSink should
        # not have to catch the driver's exceptions to use it safely.
        sink = SqliteSink(tmp_path / "evidence.db")
        sink.emit(make_record())
        sink.close()
        with pytest.raises(EvidenceSinkError, match="closed"):
            sink.read()

    def test_rejects_an_unsafe_table_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="valid SQL identifier"):
            SqliteSink(tmp_path / "evidence.db", table="evidence; DROP TABLE evidence")

    def test_custom_table_name_is_honoured(self, tmp_path: Path) -> None:
        sink = SqliteSink(tmp_path / "evidence.db", table="audit_rows")
        sink.emit(make_record())
        assert len(sink.read()) == 1
        sink.close()


# --------------------------------------------------------------------------
# BigQuery — schema, DDL, batching, failure, and the missing-SDK path.
# --------------------------------------------------------------------------


class TestBigQuerySchema:
    def test_schema_is_field_order(self) -> None:
        assert tuple(f.name for f in BIGQUERY_SCHEMA) == FIELD_ORDER

    def test_timestamp_is_a_timestamp_and_everything_else_is_a_string(self) -> None:
        types = {f.name: f.field_type for f in BIGQUERY_SCHEMA}
        assert types.pop("timestamp") == "TIMESTAMP"
        assert set(types.values()) == {"STRING"}

    def test_modes_match_the_records_nullability(self) -> None:
        modes = {f.name: f.mode for f in BIGQUERY_SCHEMA}
        assert modes == {
            "run_id": "REQUIRED",
            "parent_run_id": "NULLABLE",
            "agent_principal": "REQUIRED",
            "delegated_by": "REQUIRED",
            "intent": "REQUIRED",
            "tool": "REQUIRED",
            "args_hash": "REQUIRED",
            "policy_decision": "REQUIRED",
            "approval_ref": "NULLABLE",
            "result_hash": "NULLABLE",
            "model_id": "NULLABLE",
            "harness_version": "NULLABLE",
            "timestamp": "REQUIRED",
            "schema_version": "REQUIRED",
        }

    def test_schema_and_ddl_are_usable_with_the_sdk_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Phase 3 provisions the governed dataset from wherever the DDL review
        # happens, which is not necessarily a machine with the SDK on it. Only
        # the helper that hands back SDK objects may require it; the constant
        # and the DDL text must not. Asserting a length here would have passed
        # even if `create_table_ddl` reached for `google.cloud`.
        monkeypatch.setattr(bigquery_module, "importlib", _NoBigQueryImportlib)
        assert tuple(f.name for f in BIGQUERY_SCHEMA) == FIELD_ORDER
        ddl = create_table_ddl("agent_platform_dev", project="my-project")
        assert "`my-project.agent_platform_dev.evidence`" in ddl
        assert "PARTITION BY DATE(timestamp)" in ddl
        assert table_id("agent_platform_dev") == "agent_platform_dev.evidence"
        with pytest.raises(EvidenceSinkError):
            bigquery_schema_fields()

    def test_schema_fields_helper_requires_the_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bigquery_module, "importlib", _NoBigQueryImportlib)
        with pytest.raises(EvidenceSinkError, match=r"agent-platform\[gcp\]"):
            bigquery_schema_fields()


class TestBigQueryDdl:
    def test_ddl_names_the_fully_qualified_table(self) -> None:
        ddl = create_table_ddl("agent_platform_dev", project="my-project")
        assert "`my-project.agent_platform_dev.evidence`" in ddl

    def test_ddl_omits_the_project_when_absent(self) -> None:
        assert "`agent_platform_dev.evidence`" in create_table_ddl("agent_platform_dev")

    def test_ddl_declares_every_column_in_order(self) -> None:
        ddl = create_table_ddl("agent_platform_dev")
        positions = [ddl.index(f"\n  {name} ") for name in FIELD_ORDER]
        assert positions == sorted(positions)

    def test_ddl_marks_required_columns_not_null(self) -> None:
        ddl = create_table_ddl("agent_platform_dev")
        assert "run_id STRING NOT NULL" in ddl
        assert "parent_run_id STRING OPTIONS" in ddl
        assert "timestamp TIMESTAMP NOT NULL" in ddl

    def test_ddl_partitions_and_clusters_for_retention_and_lookup(self) -> None:
        ddl = create_table_ddl("agent_platform_dev")
        assert "PARTITION BY DATE(timestamp)" in ddl
        assert "CLUSTER BY run_id, tool" in ddl

    def test_ddl_carries_a_retention_option_when_asked(self) -> None:
        assert "partition_expiration_days = 400" in create_table_ddl(
            "agent_platform_prod", partition_expiration_days=400
        )


class TestBigQuerySink:
    def test_rows_carry_every_column_in_field_order(self) -> None:
        row = BigQuerySink.row(make_record())
        assert tuple(row) == FIELD_ORDER

    def test_row_renders_timestamp_and_decision_as_primitives(self) -> None:
        row = BigQuerySink.row(make_record(policy_decision=PolicyDecision.DENY))
        assert row["policy_decision"] == "deny"
        assert row["timestamp"] == "2026-07-25T12:00:00+00:00"
        assert json.dumps(row)

    def test_default_batch_size_writes_at_the_point_of_action(self) -> None:
        client = FakeBigQueryClient()
        sink = BigQuerySink("dev", client=client)
        sink.emit(make_record())
        assert len(client.calls) == 1
        assert sink.pending == 0
        sink.close()

    def test_batches_until_full(self) -> None:
        client = FakeBigQueryClient()
        sink = BigQuerySink("dev", client=client, batch_size=3)
        sink.emit(make_record(0))
        sink.emit(make_record(1))
        assert client.calls == []
        assert sink.pending == 2
        sink.emit(make_record(2))
        assert len(client.calls) == 1
        assert len(client.calls[0][1]) == 3
        assert sink.pending == 0
        sink.close()

    def test_close_flushes_a_partial_batch(self) -> None:
        client = FakeBigQueryClient()
        sink = BigQuerySink("dev", client=client, batch_size=10)
        sink.emit(make_record(0))
        sink.emit(make_record(1))
        assert client.calls == []
        sink.close()
        assert [row["run_id"] for row in client.rows] == ["run_0000", "run_0001"]

    def test_close_never_swallows_a_record_emitted_beside_it(self) -> None:
        # `emit` returning without raising is a promise that the row is
        # recorded. If `close` drains the buffer and only then marks itself
        # closed, a record landing in that window is buffered forever: never
        # sent, never spilled, never logged. Either outcome here is acceptable
        # — the row is delivered, or the caller is told the sink is shut — but
        # "accepted and vanished" is the one Section 6 cannot recover from.
        client = BlockingBigQueryClient()
        sink = BigQuerySink("dev", client=client, batch_size=10)
        for n in range(3):
            sink.emit(make_record(n))

        closer = threading.Thread(target=sink.close)
        closer.start()
        assert client.entered.wait(5), "close() never reached the client"

        accepted = False
        try:
            sink.emit(make_record(99))
            accepted = True
        except EvidenceSinkError:
            pass  # refusing the row is a fine answer; losing it is not
        finally:
            client.release.set()
            closer.join(5)

        delivered = {row["run_id"] for row in client.rows}
        assert sink.pending == 0, "a record was left in a buffer nothing will drain"
        if accepted:
            assert "run_0099" in delivered, "emit() accepted a record that was never sent"

    def test_close_is_idempotent_and_does_not_resend(self) -> None:
        client = FakeBigQueryClient()
        sink = BigQuerySink("dev", client=client, batch_size=10)
        sink.emit(make_record())
        sink.close()
        sink.close()
        assert len(client.calls) == 1

    def test_flush_on_an_empty_buffer_is_a_no_op(self) -> None:
        client = FakeBigQueryClient()
        sink = BigQuerySink("dev", client=client, batch_size=10)
        sink.flush()
        assert client.calls == []
        sink.close()

    def test_table_id_includes_the_project(self) -> None:
        client = FakeBigQueryClient()
        sink = BigQuerySink("dev", "audit", project="proj", client=client)
        sink.emit(make_record())
        assert client.calls[0][0] == "proj.dev.audit"
        sink.close()

    def test_rejected_rows_raise_with_the_batch_attached(self) -> None:
        # The rows must not evaporate: SpillingSink can only save what the
        # exception hands it.
        client = FakeBigQueryClient(errors=[{"index": 0, "errors": ["bad row"]}])
        sink = BigQuerySink("dev", client=client, batch_size=2)
        sink.emit(make_record(0))
        with pytest.raises(BigQueryInsertError) as caught:
            sink.emit(make_record(1))
        assert [r.run_id for r in caught.value.records] == ["run_0000", "run_0001"]
        assert isinstance(caught.value, EvidenceSinkError)

    def test_a_raising_client_is_wrapped_with_the_batch_attached(self) -> None:
        client = FakeBigQueryClient(raises=ConnectionError("no route to host"))
        sink = BigQuerySink("dev", client=client)
        with pytest.raises(BigQueryInsertError, match="no route to host") as caught:
            sink.emit(make_record(7))
        assert [r.run_id for r in caught.value.records] == ["run_0007"]

    def test_batch_size_must_be_at_least_one(self) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            BigQuerySink("dev", client=FakeBigQueryClient(), batch_size=0)

    def test_missing_sdk_raises_an_actionable_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bigquery_module, "importlib", _NoBigQueryImportlib)
        with pytest.raises(EvidenceSinkError) as caught:
            BigQuerySink("dev")
        message = str(caught.value)
        assert "google-cloud-bigquery" in message
        assert "agent-platform[gcp]" in message
        assert "JsonlSink" in message  # tells the reader what to do instead

    def test_an_injected_client_never_needs_the_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The whole sink is exercisable with the SDK unavailable, which is what
        # keeps "tests pass with no API key and no network" true.
        monkeypatch.setattr(bigquery_module, "importlib", _NoBigQueryImportlib)
        client = FakeBigQueryClient()
        sink = BigQuerySink("dev", client=client)
        sink.emit(make_record())
        sink.close()
        assert len(client.rows) == 1

    def test_missing_sdk_is_the_real_state_of_this_environment(self) -> None:
        # Nothing in this repository may need credentials or a cloud SDK to
        # test. If the gcp extra ever becomes a hard dependency, this notices.
        try:
            import google.cloud.bigquery  # noqa: F401
        except ImportError:
            with pytest.raises(EvidenceSinkError, match="google-cloud-bigquery"):
                BigQuerySink("dev")
        else:
            pytest.skip("google-cloud-bigquery is installed in this environment")

    def test_module_does_not_import_the_sdk_at_module_scope(self) -> None:
        source = Path(bigquery_module.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "google" in stripped:
                msg = f"module-scope google import found: {stripped}"
                raise AssertionError(msg)


# --------------------------------------------------------------------------
# Import discipline — the control plane may not reach into the harness.
# --------------------------------------------------------------------------


def test_importing_evidence_pulls_in_no_harness_and_no_cloud_sdk() -> None:
    # A fresh interpreter, because the test session has already imported
    # plenty. This is the mechanical form of P1: the evidence log must not know
    # the harness exists.
    program = (
        "import sys; import agent_platform.evidence; "
        "roots = {'deepagents', 'langchain', 'langchain_core', 'langgraph', 'mcp', 'google'}; "
        "banned = sorted(m for m in sys.modules "
        "if m.split('.')[0] in roots "
        "or m.startswith(('agent_platform.harness', 'agent_platform.gateway'))); "
        "print(banned)"
    )
    out = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]", f"agent_platform.evidence pulled in {out.stdout.strip()}"
