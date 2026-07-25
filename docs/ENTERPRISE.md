# Enterprise adoption

What a security, privacy, or architecture review will ask, answered in one
place. Written for a regulated environment, where "we'll work that out later"
is the answer that stops an adoption.

Where something is not decided, this document says so rather than implying it
is handled. An honest gap is reviewable; a vague reassurance is not.

---

## 1. What this is, and what it is not

**Is:** a control plane that sits between an agent and internal systems. It
exposes internal knowledge through a governed MCP tool surface, enforces policy
at the tool boundary, and writes an append-only evidence record of every tool
call.

**Is not:** a coding assistant, a model, a model gateway, an inference proxy, or
a replacement for an IDE assistant. It does not host models and it does not
route prompts.

It is a **Phase 1 pilot implementation**. It is not production-hardened, and
Section 6 below lists exactly what is missing.

## 2. Data classification

This is usually the first question, and the design answers it deliberately.

### The evidence log stores hashes, not payloads

Tool arguments and results are reduced to `sha256:<hex>` before storage. The
evidence log therefore does **not** contain the content an agent read or wrote.
It records *that* a call happened, by whom, under what decision, and whether two
calls were identical.

| Field | Classification | Notes |
|---|---|---|
| `run_id`, `parent_run_id` | Internal | Opaque identifiers |
| `agent_principal` | Internal | Non-human identity string |
| `delegated_by` | **Personal data** | Identifies a human. Usually a username or email |
| `intent` | **Potentially sensitive** | Free text: the task as stated by a human. See below |
| `tool` | Internal | Tool name |
| `args_hash`, `result_hash` | Internal | Digest only. Not reversible |
| `policy_decision`, `approval_ref` | Internal | |
| `model_id`, `harness_version` | Internal | |
| `timestamp` | Internal | |

**Two fields need a decision before production:**

- **`delegated_by` is personal data.** Under GDPR-like regimes it is subject to
  retention limits and subject-access. Decide whether to store the identifier
  directly or a pseudonym resolvable through your IAM system.
- **`intent` is free text a human typed.** Nothing prevents someone pasting a
  customer identifier into a task description. If your environment cannot
  tolerate that risk, either hash `intent` as well (losing readability) or apply
  the same classification to it as to a ticket description.

The hashing decision is deliberate and documented in `docs/evidence-schema.md`:
it avoids a data-classification conversation before there is anything to govern.
It is a Phase 1 choice, not a permanent one. If you later need payload retention
for control evidence, that is a schema change *and* a classification review.

### Knowledge source

`data/knowledge/*.yaml` in this repository is **fabricated sample data** —
invented service names, owners and controls, used by the tests. It contains
nothing real. Point `GATEWAY_KNOWLEDGE_DIR` at your own source and classify that
according to its own contents.

### What never leaves the boundary

The gateway runs in your environment. Knowledge lookups are local. No component
here sends data to a third party. The only outbound call is to whichever model
provider you configure, made by the harness — and the harness sends the model
whatever the *tool results* contain, which is the same exposure as any agent.
The gateway's job is to make that surface reviewable, not to eliminate it.

## 3. Deployment model

| | Phase 1 (as shipped) | Production target (Phase 3+) |
|---|---|---|
| Gateway | local process, stdio MCP | Cloud Run / container, private ingress |
| Execution | local shell / in-process | sandbox backend |
| Evidence | JSONL or SQLite on disk | BigQuery under formal retention |
| Model | explicit provider, no default | Vertex or equivalent, in-region |
| Identity | environment variables | workload identity, short-lived credentials |

Every layer is independently deployable and independently killable (P5). The
gateway runs on a host with no agent framework installed; CI proves this by
installing without the harness extra and asserting the control plane still
imports.

## 4. Identity and access

**Current state, stated plainly: `agent_principal` is an attribution field, not
an authentication result.** MCP stdio carries no caller identity, so the gateway
records the principal configured in its own environment. A gateway launched by
an untrusted process records whatever that process claims.

This is acceptable for a single-team pilot on a developer machine. It is **not**
acceptable for multi-tenant or unattended operation. Closing it is Phase 4's
machine-identity work, and it is the single largest gap between this and
production. See `docs/claude-code.md` for how it manifests with a second
harness.

Authorisation *is* real: `AllowlistPolicy` is default-deny, evaluated inside
gateway dispatch before any tool body runs, and cannot be bypassed by prompt
content or by removing harness middleware.

## 5. Supply chain

- **Apache-2.0** licensed. `deepagents` (MIT), `langchain` (MIT), `mcp` (MIT)
  are compatible.
- **The harness is pinned** to `deepagents==0.6.12`, deliberately (§7.1).
  Dependabot is configured to *ignore* it: a breaking release is a scheduling
  decision at a quarterly review, not an automated PR.
- **§7.1 recommends vendoring** the harness into an internal registry at a
  pinned SHA. MIT permits it. This repository does not do that — it is a
  deployment decision for the adopting organisation, and the pin is the
  prerequisite.
- **No hosted third-party service sits in the runtime or audit path.** This is
  a hard rule from §3.4. An open licence protects against code withdrawal, not
  service withdrawal.
- Cloud SDKs are optional extras (`[gcp]`), imported lazily. The default install
  reaches nothing outside your machine.

## 6. What is missing before production

Not a roadmap — a review checklist. Each of these would be flagged by a
competent security review, so they are listed rather than waited for.

| Gap | Impact | Where it is closed |
|---|---|---|
| Agent identity is unauthenticated | Cannot attribute actions in a multi-tenant deployment | Phase 4 |
| No retention policy on the evidence log | Personal data (`delegated_by`) retained indefinitely | Phase 3, needs Legal/Records |
| No access control on the evidence store | Local file in Phase 1 | Phase 3, governed dataset |
| No encryption at rest configured | Inherits the filesystem or BigQuery default | Deployment concern |
| Execution is unsandboxed | `LocalShellBackend` runs on the host | Phase 3 sandbox backend |
| No rate limiting or quota on gateway tools | A looping agent can hammer a knowledge source | Not yet designed |
| Schema-invalid MCP calls leave no evidence row | A malformed call is refused by the transport before policy | Known gap, see README |
| No SBOM published | Some procurement processes require one | Generate at release |

## 7. Operating it

```bash
agent-platform doctor          # resolved config, wired extras, where evidence lands
agent-platform evidence stats  # rows, runs, tools, decisions
agent-platform evidence tail   # most recent rows
```

**Evidence durability.** All writes go through `SpillingSink`. If the primary
store fails, the record is appended to a local spill file and the agent keeps
running. Set `EVIDENCE_FAIL_CLOSED=true` to invert that — the agent stops rather
than continuing without durable evidence. That is the correct posture once the
log is control evidence rather than good practice; it is not the pilot default,
because a pilot that dies on a transient outage produces no evidence at all.

**Monitor the spill file.** A non-empty spill means the primary sink is failing.
Nothing here alerts on that yet.

## 8. Verifying the claims

Every architectural claim in this document is enforced by a test or a CI job:

```bash
python -m pytest -q                    # full suite, no credentials, no network
python -m ruff check src tests
```

CI additionally installs the project *without* the harness extra to prove the
control plane is framework-free (P1), greps for the §3.4 banned constructions,
checks that committed config embeds no absolute paths, and measures the custom
middleware budget against the 1,500-line ceiling (W2).

If you are evaluating this, run the suite first. It is fast, needs nothing, and
the tests are written to be read.
