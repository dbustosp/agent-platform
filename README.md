# Internal Agent Platform

> The agent runtime is replaceable. The context and the evidence are ours.

Phase 1 implementation of [`docs/PLAN.md`](docs/PLAN.md) — a governed context and
evidence gateway that lets agents operate on systems and data that cannot leave
our cloud boundary, and produces an auditable record of everything they do.

This is **not** a coding agent. We already pay for one. This is the layer
beneath any agent.

## What is here

| Layer | Package | Owned |
|---|---|---|
| Evidence log | `agent_platform.evidence` | Forever |
| Agent identity | `agent_platform.identity` | Forever |
| Policy / AuthZ | `agent_platform.policy` | Forever |
| Context gateway (MCP) | `agent_platform.gateway` | Forever |
| Skills | `skills/*/SKILL.md` in Git | Forever |
| Harness (deepagents) | `agent_platform.harness` | Expected to be replaced in 12–24 months |
| Harness (Claude Code) | `agent_platform.claude_code` | Second harness — see below |

The line between the first five rows and the last one is the architecture.
Everything above it we own; the harness we expect to throw away, and the code is
arranged so that costs a sprint rather than a program.

## Quick start

No credentials and no network are needed to run or test anything below.

```bash
python3.13 -m venv .venv && ./.venv/bin/python -m pip install -e '.[gateway,harness,dev]'
```

Run the test suite:

```bash
./.venv/bin/python -m pytest
```

Copy the config template and edit if you want anything other than local defaults:

```bash
cp .env.example .env
```

## Using it

```bash
./.venv/bin/python -m agent_platform.cli doctor        # what is wired, what is missing
```

`doctor` reports the resolved identity, where evidence will land, which optional
extras are installed, and whether the knowledge source loads. Run it first.

```bash
./.venv/bin/python -m agent_platform.cli gateway serve
```

Serves the three read tools over stdio MCP. This is the composition root: the
server module itself refuses to run standalone, because choosing an evidence
sink is a deployment decision it must not make for you.

```bash
./.venv/bin/python -m agent_platform.cli run "which controls apply to a schema migration on checkout-api?"
```

Runs the agent on one task. Defaults to a real MCP connection to the gateway
(`--in-process` skips the subprocess; same policy, same evidence). Requires
`AGENT_MODEL_PROVIDER=anthropic` or `vertex` plus `AGENT_MODEL_NAME` — the
`fake` provider replays a script and is refused here rather than allowed to
imitate real work.

```bash
./.venv/bin/python -m agent_platform.cli evidence tail -n 20
./.venv/bin/python -m agent_platform.cli evidence stats
```

Phase 1's exit criterion is "evidence rows accumulating". These are how you
check, and they are not the dashboard Appendix A defers — they are how you find
out whether there is yet anything worth putting on one.

## Design rules that are enforced in code, not in review

- **P1 — the harness gets a client, never a store.** `evidence`, `identity`, and
  `policy` do not import `deepagents`, `langchain`, or any cloud SDK. Only
  `agent_platform.harness` may.
- **P2 — enforce at the tool boundary.** Policy is checked inside gateway
  dispatch. Deleting every middleware in the harness changes what is logged, not
  what is allowed.
- **P3 — evidence is emitted at the point of action.** Not reconstructed from
  traces. If the tracing vendor vanished tomorrow the audit trail is unaffected,
  because no tracer ever writes it.
- **P4 — ephemeral state may be framework-owned; semantic state may not.**
  Thread state and scratch files belong to the harness. Skills and evidence do
  not.
- **P5 — every layer independently deployable and independently killable.** The
  gateway runs without the harness installed. Install extras reflect this.

Two structural guards exist because the plan names the failure modes explicitly
in §3.4: `build_backend()` cannot construct a `StoreBackend` without an explicit
`store=`, and `build_agent()` refuses to assemble an agent whose middleware
stack lacks `EvidenceMiddleware`. See [`docs/portability-contract.md`](docs/portability-contract.md).

## Scope

This repository implements **Phase 0 tooling and Phase 1 only**.

Phase 1's "do not build" list is binding and has been honoured: there are no
sandboxes, no async triggers, no GitHub App, no PR automation, no dashboard, no
memory service, no portability test, and no governance review here. Their
absence is a decision, not an omission — see Appendix A of the plan and
[`docs/portability-contract.md`](docs/portability-contract.md#what-is-deliberately-not-enforced-yet).

Phase 0 is organisational work that cannot be done in a repository. The
worksheet with its exit criteria is at
[`docs/phase-0-baseline.md`](docs/phase-0-baseline.md), and it is blank.

## Two harnesses, one evidence log

Claude Code runs against the same gateway, under the same policy, writing the
same records to the same sink:

```bash
./.venv/bin/python -m agent_platform.cli claude-code install
```

Adding it required **no schema change and one module** — the `PostToolUse` hook
that stands in for `EvidenceMiddleware`. Gateway tools, policy, and skills all
came across untouched, because `SKILL.md` is Claude Code's native format and the
gateway was already an MCP server.

That is P1's test passing on real evidence rather than on assertion, and it is
Section 7.2's number: **harness switching cost well under one sprint, verified
2026-07-25.** Full write-up, field mapping, and the one unresolved gap are in
[`docs/claude-code.md`](docs/claude-code.md).

## Known Phase-1 gaps

Recorded because a gap you have written down is a decision, and one you have not
is a surprise. None of these block the Phase 1 exit criteria.

- **Identity over MCP is process-scoped.** MCP stdio carries no caller identity,
  so the gateway records the principal configured in *its own* environment, not
  one asserted by the connecting client. When we launch the gateway we pass run
  identity down through the environment, so they agree — but a gateway launched
  by Claude Code from `.mcp.json` gets no such context, and its rows cannot be
  joined to the Claude Code session. Section 7.4 and Phase 4's machine identity
  are what close this properly; see
  [`docs/claude-code.md`](docs/claude-code.md#known-gap-gateway-rows-do-not-join-to-the-session).
  Pinned by `test_mcp_identity_comes_from_the_server_environment`.
- **A schema-invalid MCP call leaves no evidence row.** FastMCP validates
  arguments against the tool signature before our handler runs, so
  `call_tool("recall", {})` is refused by the transport without reaching policy.
  Closing it means hand-rolling validation and degrading the model-facing tool
  schema, which is a bad trade at this stage.
- **A subagent's `run_id` is per-agent, not per-delegation.** Lineage
  (`parent_run_id`) is correct; repeated delegations inside one task share a
  child run id. `deepagents` 0.6.12 exposes no per-invocation hook.
- **`EvidenceMiddleware` de-duplicates by tool name.** A harness-local tool
  named `recall` would be skipped as though the gateway had logged it.
- **The sample knowledge directory is checkout-relative.** A non-editable
  install has to set `GATEWAY_KNOWLEDGE_DIR`; `data/` is not packaged into the
  wheel.

## Documentation

| Document | What it is for |
|---|---|
| [`docs/PLAN.md`](docs/PLAN.md) | The plan being implemented |
| [`docs/claude-code.md`](docs/claude-code.md) | The §7.2 portability test: Claude Code as a second harness |
| [`docs/evidence-schema.md`](docs/evidence-schema.md) | Section 6 as a reference for Legal and Records |
| [`docs/portability-contract.md`](docs/portability-contract.md) | Section 3.4 as an operational document |
| [`docs/phase-0-baseline.md`](docs/phase-0-baseline.md) | Blank Phase 0 worksheet |
| [`docs/VERIFIED-API.md`](docs/VERIFIED-API.md) | `deepagents` 0.6.12 API facts read from the installed wheel |
| [`docs/ENTERPRISE.md`](docs/ENTERPRISE.md) | Data classification, deployment, identity, supply chain — what a security review will ask |
| [`SECURITY.md`](SECURITY.md) | Reporting vulnerabilities; the security properties this project asserts |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Setup, and the constraints that are not style preferences |

## Status and licence

Pre-1.0. This is a **Phase 1 pilot implementation**, not a production system —
[`docs/ENTERPRISE.md §6`](docs/ENTERPRISE.md#6-what-is-missing-before-production)
lists what a security review would flag, rather than leaving you to find it.

`docs/PLAN.md` describes a **hypothetical organisation**. Its assumptions,
regulatory considerations and vendor decisions are illustrative and are not the
position of any real company.

Licensed under [Apache-2.0](LICENSE).
