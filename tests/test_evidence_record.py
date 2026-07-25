"""The evidence record must match Section 6 exactly.

Section 6 is the contract with a future auditor, not an internal data structure.
A field that quietly changes name or nullability three months in makes the rows
written before the change unreadable next to the rows written after it, which is
the failure the plan calls unrefinanceable. These tests pin the schema so that
changing it is a deliberate act with a red test attached.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent_platform.evidence.record import (
    FIELD_ORDER,
    SCHEMA_VERSION,
    EvidenceRecord,
    PolicyDecision,
    new_run_id,
    utc_now,
)

#: Transcribed from Section 6 of PLAN.md, in the order the plan lists them.
SECTION_6_FIELDS = (
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
)


def make_record(**overrides) -> EvidenceRecord:
    base = {
        "run_id": "run_abc",
        "agent_principal": "agent://local/pilot",
        "delegated_by": "tester",
        "intent": "look up who owns checkout-service",
        "tool": "lookup_service_context",
        "args_hash": "sha256:deadbeef",
        "policy_decision": PolicyDecision.ALLOW,
    }
    base.update(overrides)
    return EvidenceRecord(**base)


class TestSchemaCoverage:
    def test_every_section_6_field_exists(self) -> None:
        present = {f.name for f in fields(EvidenceRecord)}
        missing = [name for name in SECTION_6_FIELDS if name not in present]
        assert not missing, f"Section 6 fields absent from EvidenceRecord: {missing}"

    def test_field_order_covers_the_dataclass_exactly(self) -> None:
        assert set(FIELD_ORDER) == {f.name for f in fields(EvidenceRecord)}
        assert len(FIELD_ORDER) == len(set(FIELD_ORDER))

    def test_field_order_follows_section_6(self) -> None:
        # schema_version is ours, not the plan's; everything before it is the
        # plan's order verbatim.
        assert FIELD_ORDER[: len(SECTION_6_FIELDS)] == SECTION_6_FIELDS
        assert FIELD_ORDER[len(SECTION_6_FIELDS) :] == ("schema_version",)

    def test_policy_decision_is_the_three_values_the_plan_names(self) -> None:
        assert {d.value for d in PolicyDecision} == {"allow", "deny", "escalate"}

    def test_schema_version_defaults_onto_every_record(self) -> None:
        assert make_record().schema_version == SCHEMA_VERSION


class TestTimestamps:
    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            make_record(timestamp=datetime(2026, 7, 25, 12, 0, 0))

    def test_utc_timestamp_is_accepted(self) -> None:
        record = make_record(timestamp=datetime(2026, 7, 25, 12, 0, tzinfo=UTC))
        assert record.timestamp.tzinfo is UTC

    def test_non_utc_offset_is_accepted_and_preserved(self) -> None:
        # tz-awareness is the requirement, not UTC specifically: the offset
        # survives the round trip so the instant is never ambiguous.
        tz = timezone(timedelta(hours=2))
        record = make_record(timestamp=datetime(2026, 7, 25, 12, 0, tzinfo=tz))
        assert EvidenceRecord.from_dict(record.to_dict()).timestamp == record.timestamp

    def test_default_timestamp_is_tz_aware(self) -> None:
        assert make_record().timestamp.tzinfo is not None

    def test_utc_now_is_tz_aware(self) -> None:
        assert utc_now().tzinfo is not None


class TestRequiredFields:
    @pytest.mark.parametrize(
        "field_name",
        ["run_id", "agent_principal", "delegated_by", "tool", "args_hash"],
    )
    def test_empty_required_field_is_rejected(self, field_name: str) -> None:
        with pytest.raises(ValueError, match=field_name):
            make_record(**{field_name: ""})

    @pytest.mark.parametrize(
        "field_name",
        ["parent_run_id", "approval_ref", "result_hash", "model_id", "harness_version"],
    )
    def test_optional_fields_default_to_none(self, field_name: str) -> None:
        assert getattr(make_record(), field_name) is None

    def test_record_is_frozen(self) -> None:
        record = make_record()
        with pytest.raises(FrozenInstanceError):
            record.tool = "something_else"  # type: ignore[misc]


class TestRoundTrip:
    def test_full_record_round_trips(self) -> None:
        original = make_record(
            parent_run_id="run_parent",
            approval_ref="approval-17",
            result_hash="sha256:cafe",
            model_id="vertex/gemini-x.y",
            harness_version="deepagents==0.6.12",
            policy_decision=PolicyDecision.ESCALATE,
        )
        assert EvidenceRecord.from_dict(original.to_dict()) == original

    def test_minimal_record_round_trips(self) -> None:
        original = make_record()
        assert EvidenceRecord.from_dict(original.to_dict()) == original

    def test_to_dict_is_json_serialisable(self) -> None:
        payload = make_record().to_dict()
        assert json.loads(json.dumps(payload)) == payload

    def test_to_dict_emits_every_field_and_nothing_else(self) -> None:
        # Coverage, not order. `to_dict` iterates the dataclass declaration
        # order (required fields first, then defaulted ones), which is *not*
        # FIELD_ORDER despite what its docstring claims. Key order is cosmetic
        # in JSON, so nothing breaks — but the tabular sinks project through
        # FIELD_ORDER explicitly rather than trusting this dict's ordering,
        # and `test_sinks.py` pins that.
        assert set(make_record().to_dict()) == set(FIELD_ORDER)

    def test_enum_and_datetime_serialise_to_primitives(self) -> None:
        payload = make_record(policy_decision=PolicyDecision.DENY).to_dict()
        assert payload["policy_decision"] == "deny"
        assert isinstance(payload["timestamp"], str)

    def test_from_dict_ignores_unknown_keys(self) -> None:
        # A row written by a later schema version must still load, minus the
        # fields this build does not know about. The alternative is an audit
        # tool that crashes on its own history.
        payload = make_record().to_dict()
        payload["some_future_column"] = "value"
        assert EvidenceRecord.from_dict(payload).tool == "lookup_service_context"

    def test_from_dict_accepts_already_typed_values(self) -> None:
        record = EvidenceRecord.from_dict(
            {
                "run_id": "run_1",
                "agent_principal": "agent://local/pilot",
                "delegated_by": "tester",
                "intent": "i",
                "tool": "t",
                "args_hash": "sha256:00",
                "policy_decision": PolicyDecision.ALLOW,
                "timestamp": datetime(2026, 7, 25, tzinfo=UTC),
            }
        )
        assert record.policy_decision is PolicyDecision.ALLOW


class TestRunIds:
    def test_run_ids_are_unique_and_prefixed(self) -> None:
        ids = {new_run_id() for _ in range(100)}
        assert len(ids) == 100
        assert all(rid.startswith("run_") for rid in ids)
