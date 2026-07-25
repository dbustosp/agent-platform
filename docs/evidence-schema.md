# Evidence schema

Section 6 of the plan, as the reference document for Legal, Records, and anyone
reviewing what the platform retains.

> **Why this is the one non-negotiable:** most technical debt is refinanceable.
> This is not. If the pilot works and someone asks in month four what it has
> been doing, "we will add logging now" leaves us with no history for exactly
> the period that matters.

One record is written per tool call, from run one. In Phase 1 nobody reviews
these records. They are just written.

## Fields

| Field | Type | Null? | Meaning |
|---|---|---|---|
| `run_id` | string | no | Stable across a task |
| `parent_run_id` | string | yes | Subagent lineage; null for a top-level run |
| `agent_principal` | string | no | The non-human identity acting |
| `delegated_by` | string | no | The human on whose authority it acts |
| `intent` | string | no | The task as stated |
| `tool` | string | no | Tool name |
| `args_hash` | string | no | `sha256:<hex>` of canonicalised arguments — hash, not payload |
| `policy_decision` | enum | no | `allow` \| `deny` \| `escalate` |
| `approval_ref` | string | yes | HITL approval id, where applicable |
| `result_hash` | string | yes | `sha256:<hex>` of the result — hash, not payload. Null if the call did not produce one |
| `model_id` | string | yes | Model and version |
| `harness_version` | string | yes | e.g. `deepagents==0.6.12` |
| `timestamp` | timestamp | no | UTC, timezone-aware |
| `schema_version` | string | no | Added by this implementation; see below |

### `schema_version` is an addition to the plan

Section 6 does not list it. It is here because the plan's own logic requires it:
the record outlives the harness, which means it also outlives this schema. A
record with no version is a record that cannot be migrated without guessing when
it was written. One column now avoids a data-archaeology exercise later.

Current value: `"1"`.

### `harness_version` is a field, not a dependency

This is the load-bearing sentence in Section 6. We record which harness produced
a row. We never ask the harness what a row means, and nothing in the read path
imports the harness. When `deepagents` is replaced, old rows keep their old
value and stay readable.

## Why payloads are hashed

Hashing rather than storing avoids a data-classification conversation before
there is anything to govern. It preserves the questions actually worth asking in
Phase 1:

- Did this exact call happen before? (compare `args_hash`)
- Did the same inputs produce the same output? (compare `result_hash`)
- How often does this tool get called, by whom, under what decision?

It does not preserve "what did the agent actually see", which is deliberate.
Phase 3 moves the dataset under formal retention and access control, and that is
the right time to decide whether any payload is retained.

### Hash canonicalisation

`sha256` over canonical JSON: keys sorted, no insignificant whitespace, sets
sorted by their canonical form, bytes hashed separately, and unserialisable
objects degraded to a type-tagged repr rather than raising. Two structurally
identical calls hash identically regardless of dict insertion order. The empty
sentinel `sha256:__empty__` marks absent arguments and is distinct from the hash
of `None`, which is a value the caller actually passed.

## Who writes a row

Exactly one component authors each record, so there are no duplicates and no
ambiguity about which decision a row reflects.

| Tool | Author | Why |
|---|---|---|
| Gateway tools (`lookup_service_context`, `get_control_requirements`, `recall`, `record_evidence`) | The gateway | It is the point of action and it holds the authoritative policy decision (P3) |
| Everything else the harness calls (shell, filesystem, subagent spawns) | `EvidenceMiddleware` | The gateway never sees these calls |

`EvidenceMiddleware` is given the set of gateway-owned tool names and skips
them. Removing the middleware stops rows being written for non-gateway tools; it
does not stop policy being enforced, because enforcement lives in gateway
dispatch rather than in the harness (P2).

Denied and escalated calls produce records too. A refusal is precisely the thing
an audit asks about, and a log that only contains successes is not an audit
trail.

## Storage

Phase 1 is local-first. Section 4.4 puts BigQuery on both sides of the
local→cloud move, so the sink interface is the same in both:

| | Phase 1 (local) | Phase 3 (cloud) |
|---|---|---|
| Default sink | JSONL on disk | BigQuery, governed dataset |
| Also available | SQLite (queryable), BigQuery dev dataset | — |
| Retention | none configured | formal retention and access control |

`SpillingSink` wraps the primary: a sink failure appends the row to a local
spill file and logs, rather than losing the row or killing the agent. The risk
register rates "evidence gap discovered late" as Severe, and an outage in the
store is the most likely way to create one.

`fail_closed=True` inverts that — the agent stops rather than continuing without
durable evidence. That becomes the correct posture if watch item W5 fires and
the log turns into control evidence. It is not the Phase 1 default.

## Open questions owned outside engineering

From Section 10, unresolved and blocking nothing in Phase 1:

- **Retention period** — Legal and Records input required
- **Access model** — who may read the evidence dataset, and under what process

Both must be answered before the Phase 3 move to a governed dataset.
