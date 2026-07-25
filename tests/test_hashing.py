"""Canonical hashing has exactly one job: be stable.

`args_hash` exists to answer "have we done this before?". It answers nothing if
two structurally identical calls hash differently because a dict was built in a
different order, or because a set iterated differently in a different process.
These tests pin that stability, and pin the other half of the contract too — an
exotic tool result must degrade to a weaker digest rather than raise, because a
crashed agent with no evidence row is strictly worse than an imprecise one.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys

import pytest

from agent_platform.evidence.hashing import (
    ALGORITHM,
    EMPTY,
    hash_args,
    hash_payload,
    hash_result,
)


class TestStability:
    def test_key_order_does_not_change_the_digest(self) -> None:
        a = {"service": "checkout", "env": "prod", "depth": 2}
        b = {"depth": 2, "env": "prod", "service": "checkout"}
        assert hash_payload(a) == hash_payload(b)

    def test_nested_key_order_does_not_change_the_digest(self) -> None:
        a = {"filter": {"owner": "team-a", "tier": 1}, "limit": 10}
        b = {"limit": 10, "filter": {"tier": 1, "owner": "team-a"}}
        assert hash_payload(a) == hash_payload(b)

    def test_deeply_nested_structures_hash(self) -> None:
        payload = {"a": [{"b": [{"c": {"d": [1, 2, {"e": None}]}}]}]}
        assert hash_payload(payload) == hash_payload(payload)

    def test_set_ordering_does_not_change_the_digest(self) -> None:
        assert hash_payload({"tags": {"c", "a", "b"}}) == hash_payload({"tags": {"b", "c", "a"}})

    def test_frozenset_matches_set(self) -> None:
        assert hash_payload({"a", "b"}) == hash_payload(frozenset({"b", "a"}))

    def test_mixed_type_set_still_hashes(self) -> None:
        # Sorting raw set members would raise on mixed types; the canonical
        # form has to be sorted instead.
        assert hash_payload({1, "a", None}) == hash_payload({None, "a", 1})

    def test_list_order_is_significant(self) -> None:
        assert hash_payload([1, 2, 3]) != hash_payload([3, 2, 1])

    def test_tuples_and_lists_agree(self) -> None:
        # Deliberate: a payload that round-trips through JSON comes back as a
        # list, and the digest must not depend on which side of that trip the
        # value was hashed.
        assert hash_payload((1, 2)) == hash_payload([1, 2])

    def test_digest_is_stable_across_processes(self) -> None:
        # PYTHONHASHSEED randomises set and str hashing per process. If any of
        # that leaked into the digest, this is where it shows up.
        payload_src = "{'tags': {'c', 'a', 'b'}, 'n': 3, 'nested': {'z': 1, 'a': [1, {'k': 'v'}]}}"
        expected = hash_payload(ast.literal_eval(payload_src))
        program = (
            "from agent_platform.evidence.hashing import hash_payload;"
            f"print(hash_payload({payload_src}))"
        )
        digests = set()
        for seed in ("0", "1", "424242"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            out = subprocess.run(
                [sys.executable, "-c", program],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            digests.add(out.stdout.strip())
        assert digests == {expected}


class TestFormat:
    def test_digest_is_algorithm_prefixed_hex(self) -> None:
        digest = hash_payload({"a": 1})
        algorithm, _, hexdigest = digest.partition(":")
        assert algorithm == ALGORITHM
        assert len(hexdigest) == 64
        assert set(hexdigest) <= set("0123456789abcdef")

    def test_different_payloads_differ(self) -> None:
        assert hash_payload({"a": 1}) != hash_payload({"a": 2})

    def test_string_and_number_do_not_collide(self) -> None:
        assert hash_payload("1") != hash_payload(1)


class TestBytes:
    def test_bytes_hash_by_content(self) -> None:
        assert hash_payload(b"hello") == hash_payload(b"hello")
        assert hash_payload(b"hello") != hash_payload(b"world")

    def test_bytes_do_not_collide_with_their_repr(self) -> None:
        assert hash_payload(b"hello") != hash_payload("hello")

    def test_nested_bytes_hash(self) -> None:
        assert hash_payload({"blob": b"\x00\xff"}) == hash_payload({"blob": b"\x00\xff"})


class TestDegradation:
    def test_unserialisable_object_degrades_instead_of_raising(self) -> None:
        class Opaque:
            def __repr__(self) -> str:
                return "<Opaque stable>"

        assert hash_payload(Opaque()) == hash_payload(Opaque())

    def test_object_exposing_to_dict_is_used_structurally(self) -> None:
        class HasToDict:
            def to_dict(self) -> dict[str, int]:
                return {"a": 1}

        assert hash_payload(HasToDict()) == hash_payload({"a": 1})

    def test_object_whose_to_dict_raises_still_hashes(self) -> None:
        class Hostile:
            def to_dict(self):
                raise RuntimeError("no")

            def __repr__(self) -> str:
                return "<Hostile>"

        assert hash_payload(Hostile()).startswith(f"{ALGORITHM}:")

    @pytest.mark.parametrize(
        "payload",
        [
            complex(1, 2),
            object,
            Exception("tool blew up"),
            {"nested": complex(0, 1)},
            [complex(3, 4), {"k": Exception("x")}],
        ],
        ids=["complex", "type", "exception", "nested", "in-list"],
    )
    def test_json_hostile_payloads_still_produce_a_digest(self, payload: object) -> None:
        # None of these survive json.dumps. A tool that returns one must still
        # leave an evidence row behind.
        assert hash_payload(payload).startswith(f"{ALGORITHM}:")

    def test_degraded_digests_are_still_deterministic(self) -> None:
        assert hash_payload(complex(1, 2)) == hash_payload(complex(1, 2))
        assert hash_payload(complex(1, 2)) != hash_payload(complex(2, 1))

    def test_hash_result_of_an_arbitrary_object_never_raises(self) -> None:
        class ToolOutput:
            def __init__(self) -> None:
                self.rows = [{"owner": "team-a"}]

            def __repr__(self) -> str:
                return f"ToolOutput({self.rows})"

        assert hash_result(ToolOutput()) == hash_result(ToolOutput())


class TestSentinels:
    def test_empty_is_distinct_from_the_digest_of_none(self) -> None:
        # "no arguments were passed" and "the argument was None" are different
        # facts about what the agent did.
        assert hash_payload(None) != EMPTY
        assert hash_result(None) != EMPTY

    def test_hash_args_uses_the_sentinel_for_absent_and_empty(self) -> None:
        assert hash_args(None) == EMPTY
        assert hash_args({}) == EMPTY

    def test_hash_args_hashes_real_arguments(self) -> None:
        assert hash_args({"service": "checkout"}) != EMPTY

    def test_hash_result_and_hash_args_agree_on_the_same_payload(self) -> None:
        payload = {"service": "checkout"}
        assert hash_args(payload) == hash_result(payload)

    def test_empty_is_not_a_valid_sha256_digest(self) -> None:
        # It must be impossible for a real payload to collide with the
        # sentinel.
        assert f"{ALGORITHM}:__empty__" == EMPTY
