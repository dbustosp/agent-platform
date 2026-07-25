"""Regression tests for evidence that was being accepted and then lost.

Both failures below were found by review, not by the original test suite, and
both create the failure mode Section 6 calls unbackfillable: a caller is told
the record was written, and it was not.
"""

from __future__ import annotations

import json

import pytest

from agent_platform.evidence.record import FIELD_ORDER, EvidenceRecord, PolicyDecision
from agent_platform.evidence.sink import SpillingSink
from agent_platform.evidence.sinks.bigquery import BigQueryInsertError, BigQuerySink


def _record(n: int) -> EvidenceRecord:
    return EvidenceRecord(
        run_id=f"run_{n}",
        agent_principal="agent://test",
        delegated_by="tester",
        intent="regression",
        tool="recall",
        args_hash=f"sha256:{n:064d}",
        policy_decision=PolicyDecision.ALLOW,
    )


class _AlwaysFailsClient:
    """Stands in for a BigQuery client whose insert never succeeds."""

    def __init__(self) -> None:
        self.attempts = 0

    def insert_rows_json(self, *args: object, **kwargs: object) -> list[dict]:
        self.attempts += 1
        return [{"index": 0, "errors": [{"reason": "backendError"}]}]


def test_a_failed_batch_spills_every_record_not_just_the_last(tmp_path):
    """A batching sink fails N rows at once; all N must reach the spill.

    Before the fix, `SpillingSink` wrote only the single record it was handed,
    so a batch of 3 lost 2 rows silently on every failed flush.
    """
    spill = tmp_path / "spill.jsonl"
    primary = BigQuerySink("ds", "evidence", client=_AlwaysFailsClient(), batch_size=3)
    sink = SpillingSink(primary, spill)

    for n in range(3):
        sink.emit(_record(n))

    assert sink.spilled_count == 3, "every record in the failed batch must be spilled"
    lines = spill.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert {json.loads(line)["run_id"] for line in lines} == {"run_0", "run_1", "run_2"}
    # And the spill must be replayable, not just written.
    assert [EvidenceRecord.from_dict(json.loads(line)).run_id for line in lines] == [
        "run_0",
        "run_1",
        "run_2",
    ]


def test_a_failed_flush_on_close_spills_the_buffer(tmp_path):
    """close() flushes a batching sink. If that flush fails, the rows must survive."""
    spill = tmp_path / "spill.jsonl"
    primary = BigQuerySink("ds", "evidence", client=_AlwaysFailsClient(), batch_size=10)
    sink = SpillingSink(primary, spill)

    for n in range(4):
        sink.emit(_record(n))
    assert sink.spilled_count == 0, "nothing has been flushed yet, so nothing has failed yet"

    sink.close()

    assert sink.spilled_count == 4, "the buffer that failed to flush must land in the spill"
    assert len(spill.read_text(encoding="utf-8").strip().splitlines()) == 4


def test_fail_closed_still_raises_on_a_failed_close(tmp_path):
    primary = BigQuerySink("ds", "evidence", client=_AlwaysFailsClient(), batch_size=10)
    sink = SpillingSink(primary, tmp_path / "spill.jsonl", fail_closed=True)
    sink.emit(_record(0))

    with pytest.raises(Exception) as excinfo:
        sink.close()
    assert "fail_closed" in str(excinfo.value)
    assert sink.spilled_count == 1, "the row is spilled even when the error propagates"


def test_the_insert_error_really_does_carry_the_whole_batch():
    """Pins the contract `SpillingSink` depends on.

    If `BigQueryInsertError` ever stops carrying `.records`, the batch spill
    above degrades to single-record spilling silently. This test fails first.
    """
    records = (_record(0), _record(1))
    err = BigQueryInsertError("boom", records)
    assert err.records == records


def test_to_dict_key_order_is_the_section_6_order():
    """The docstring promised Section 6 column order; it used to emit field order."""
    keys = list(_record(0).to_dict().keys())
    assert keys == list(FIELD_ORDER)
    # parent_run_id sits second in Section 6, immediately after run_id.
    assert keys[:3] == ["run_id", "parent_run_id", "agent_principal"]


def test_policy_decision_stringifies_to_its_value():
    """A stray f-string must not put "PolicyDecision.ALLOW" in an evidence column."""
    assert f"{PolicyDecision.ALLOW}" == "allow"
    assert str(PolicyDecision.ESCALATE) == "escalate"
    assert json.dumps({"d": PolicyDecision.DENY}) == '{"d": "deny"}'
