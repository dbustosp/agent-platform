"""SpillingSink is the mitigation for the only Severe row in the risk register.

"Evidence gap discovered late — Low likelihood, Severe impact." The gap this
guards against is not a design mistake, it is a bad afternoon: BigQuery is
unreachable, the sink raises, and either the agent dies or the row vanishes.
Neither is acceptable, so the record goes to a local file and the run
continues.

These tests pin both halves of that: nothing is lost, and nothing that is lost
is lost silently.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_platform.evidence.record import EvidenceRecord, PolicyDecision
from agent_platform.evidence.sink import EvidenceSink, EvidenceSinkError, SpillingSink
from agent_platform.evidence.sinks import InMemorySink, JsonlSink

SINK_LOGGER = "agent_platform.evidence.sink"


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


class FailingSink:
    """A primary that is having the bad afternoon."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or ConnectionError("BigQuery unreachable")
        self.attempts = 0
        self.closed = 0

    def emit(self, record: EvidenceRecord) -> None:
        self.attempts += 1
        raise self.exc

    def close(self) -> None:
        self.closed += 1


class FlakySink:
    """Fails only for the runs named in `fail_runs`."""

    def __init__(self, fail_runs: set[str]) -> None:
        self.fail_runs = fail_runs
        self.accepted: list[EvidenceRecord] = []
        self.closed = 0

    def emit(self, record: EvidenceRecord) -> None:
        if record.run_id in self.fail_runs:
            msg = f"refusing {record.run_id}"
            raise RuntimeError(msg)
        self.accepted.append(record)

    def close(self) -> None:
        self.closed += 1


class TestHappyPath:
    def test_satisfies_the_protocol(self, tmp_path: Path) -> None:
        sink = SpillingSink(InMemorySink(), tmp_path / "spill.jsonl")
        assert isinstance(sink, EvidenceSink)

    def test_records_reach_the_primary_untouched(self, tmp_path: Path) -> None:
        primary = InMemorySink()
        sink = SpillingSink(primary, tmp_path / "spill.jsonl")
        records = [make_record(n) for n in range(5)]
        for record in records:
            sink.emit(record)
        assert list(primary.records) == records

    def test_a_healthy_primary_creates_no_spill_file(self, tmp_path: Path) -> None:
        spill = tmp_path / "spill.jsonl"
        sink = SpillingSink(InMemorySink(), spill)
        sink.emit(make_record())
        assert not spill.exists()
        assert sink.spilled_count == 0

    def test_close_closes_the_primary_exactly_once(self, tmp_path: Path) -> None:
        primary = FlakySink(set())
        sink = SpillingSink(primary, tmp_path / "spill.jsonl")
        sink.close()
        sink.close()
        sink.close()
        assert primary.closed == 1


class TestSpilling:
    def test_primary_failure_does_not_reach_the_caller(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The agent keeps running. That is the entire point: a pilot that dies
        # on a BigQuery hiccup produces no evidence at all.
        sink = SpillingSink(FailingSink(), tmp_path / "spill.jsonl")
        with caplog.at_level(logging.ERROR, logger=SINK_LOGGER):
            sink.emit(make_record())
        assert sink.spilled_count == 1

    def test_the_failure_is_logged_loudly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Not raising is not the same as not saying anything.
        sink = SpillingSink(FailingSink(), tmp_path / "spill.jsonl")
        with caplog.at_level(logging.ERROR, logger=SINK_LOGGER):
            sink.emit(make_record())
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_spilled_count_tracks_only_the_failures(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        primary = FlakySink({"run_0001", "run_0003"})
        sink = SpillingSink(primary, tmp_path / "spill.jsonl")
        with caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER):
            for n in range(5):
                sink.emit(make_record(n))
        assert sink.spilled_count == 2
        assert [r.run_id for r in primary.accepted] == ["run_0000", "run_0002", "run_0004"]

    def test_spill_file_is_valid_jsonl(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spill = tmp_path / "spill.jsonl"
        sink = SpillingSink(FailingSink(), spill)
        records = [make_record(n) for n in range(4)]
        with caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER):
            for record in records:
                sink.emit(record)
        lines = spill.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 4
        assert [EvidenceRecord.from_dict(json.loads(line)) for line in lines] == records

    def test_spill_file_replays_through_the_jsonl_reader(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The spill format is the JSONL sink's format on purpose: recovering a
        # spill is a concatenation, not a migration.
        spill = tmp_path / "spill.jsonl"
        sink = SpillingSink(FailingSink(), spill)
        records = [make_record(n) for n in range(3)]
        with caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER):
            for record in records:
                sink.emit(record)
        assert JsonlSink.read(spill, strict=True) == records

    def test_spill_survives_a_fully_populated_record(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spill = tmp_path / "spill.jsonl"
        sink = SpillingSink(FailingSink(), spill)
        record = make_record(
            parent_run_id="run_parent",
            approval_ref="approval-17",
            result_hash="sha256:cafe",
            model_id="vertex/gemini-x.y",
            harness_version="deepagents==0.6.12",
            policy_decision=PolicyDecision.ESCALATE,
        )
        with caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER):
            sink.emit(record)
        assert JsonlSink.read(spill, strict=True) == [record]

    def test_spill_creates_parent_directories(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spill = tmp_path / "deeply" / "nested" / "spill.jsonl"
        sink = SpillingSink(FailingSink(), spill)
        with caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER):
            sink.emit(make_record())
        assert spill.exists()

    def test_spill_appends_across_reopen(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spill = tmp_path / "spill.jsonl"
        with caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER):
            first = SpillingSink(FailingSink(), spill)
            first.emit(make_record(0))
            first.close()
            second = SpillingSink(FailingSink(), spill)
            second.emit(make_record(1))
            second.close()
        assert [r.run_id for r in JsonlSink.read(spill)] == ["run_0000", "run_0001"]
        # spilled_count is per-instance, not per-file.
        assert second.spilled_count == 1

    def test_concurrent_spills_lose_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spill = tmp_path / "spill.jsonl"
        sink = SpillingSink(FailingSink(), spill)
        threads, per_thread = 8, 20

        def worker(offset: int) -> None:
            for i in range(per_thread):
                sink.emit(make_record(offset * per_thread + i))

        with caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER):
            workers = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()

        recovered = JsonlSink.read(spill, strict=True)
        assert sink.spilled_count == threads * per_thread
        assert len(recovered) == threads * per_thread
        assert len({r.args_hash for r in recovered}) == threads * per_thread


class TestFailClosed:
    def test_fail_closed_propagates_as_an_evidence_sink_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = SpillingSink(FailingSink(), tmp_path / "spill.jsonl", fail_closed=True)
        with (
            caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER),
            pytest.raises(EvidenceSinkError),
        ):
            sink.emit(make_record())

    def test_fail_closed_still_spills_before_it_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Halting is a policy choice about the agent; it is never a reason to
        # throw the row away.
        spill = tmp_path / "spill.jsonl"
        sink = SpillingSink(FailingSink(), spill, fail_closed=True)
        record = make_record()
        with (
            caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER),
            pytest.raises(EvidenceSinkError),
        ):
            sink.emit(record)
        assert sink.spilled_count == 1
        assert JsonlSink.read(spill, strict=True) == [record]

    def test_fail_closed_chains_the_original_cause(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        original = ConnectionError("no route to host")
        sink = SpillingSink(FailingSink(original), tmp_path / "spill.jsonl", fail_closed=True)
        with (
            caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER),
            pytest.raises(EvidenceSinkError) as caught,
        ):
            sink.emit(make_record())
        assert caught.value.__cause__ is original

    def test_fail_open_is_the_phase_1_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Behaviour, not the private flag: reading `_fail_closed` back only
        # restates the constructor. What the plan actually promises is that a
        # sink outage does not reach the agent unless someone asked for that.
        spill = tmp_path / "spill.jsonl"
        primary = FailingSink()
        sink = SpillingSink(primary, spill)
        record = make_record()
        with caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER):
            sink.emit(record)  # must not raise
        assert primary.attempts == 1
        assert sink.spilled_count == 1
        assert JsonlSink.read(spill, strict=True) == [record]


class TestLastResort:
    def test_both_paths_failing_logs_critical_instead_of_raising(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Spill path is a directory, so the spill write cannot succeed either.
        # The row is lost — but it is lost at CRITICAL with its contents in the
        # log stream, which is the difference between a known gap and an
        # unknown one.
        spill = tmp_path / "spill.jsonl"
        spill.mkdir()
        sink = SpillingSink(FailingSink(), spill)
        with caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER):
            sink.emit(make_record())
        critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert critical, "a lost evidence record must be logged at CRITICAL"
        assert "EVIDENCE LOSS" in critical[0].getMessage()
        assert sink.spilled_count == 0

    def test_both_paths_failing_still_honours_fail_closed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spill = tmp_path / "spill.jsonl"
        spill.mkdir()
        sink = SpillingSink(FailingSink(), spill, fail_closed=True)
        with (
            caplog.at_level(logging.CRITICAL, logger=SINK_LOGGER),
            pytest.raises(EvidenceSinkError),
        ):
            sink.emit(make_record())


class TestComposition:
    def test_wraps_a_real_sink_end_to_end(self, tmp_path: Path) -> None:
        # The Phase 1 shape: a durable primary with a spill behind it.
        primary_path = tmp_path / "evidence.jsonl"
        spill = tmp_path / "spill.jsonl"
        primary = JsonlSink(primary_path)
        sink = SpillingSink(primary, spill)
        records = [make_record(n) for n in range(3)]
        for record in records:
            sink.emit(record)
        sink.close()
        assert JsonlSink.read(primary_path) == records
        assert not spill.exists()

    def test_closing_the_wrapper_closes_the_wrapped_sink(self, tmp_path: Path) -> None:
        primary = JsonlSink(tmp_path / "evidence.jsonl")
        sink = SpillingSink(primary, tmp_path / "spill.jsonl")
        sink.close()
        with pytest.raises(EvidenceSinkError):
            primary.emit(make_record())
