# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project is pre-1.0 and does not yet promise a stable API.

## [Unreleased]

### Added — Phase 1 (initial implementation)

- **Evidence log** (§6) — `EvidenceRecord`, canonical payload hashing, and four
  sinks: JSONL, SQLite, BigQuery, in-memory. `SpillingSink` wraps the primary so
  an outage costs a replay job rather than an audit gap.
- **Context gateway** (§3.3) — MCP server exposing three read-only tools
  (`lookup_service_context`, `get_control_requirements`, `recall`) over a
  default-deny allowlist. `record_evidence` exists on the gateway and is
  deliberately not advertised to models.
- **Policy** (P2) — evaluated inside gateway dispatch, before any tool body runs.
- **deepagents harness** (§4) — `create_deep_agent` assembly, `CompositeBackend`
  routing with a guard against store-less `StoreBackend`, and `EvidenceMiddleware`.
- **Claude Code harness** (§7.2) — `PostToolUse` hook writing the same records
  to the same sink. No schema change was required.
- **Skills** (§3.3) — `SKILL.md` files in Git, byte-compatible with Claude Code.
- **CLI** — `run`, `gateway serve`, `evidence tail|stats`, `doctor`,
  `claude-code install|hook`.
- Enterprise baseline: Apache-2.0, CI across Python 3.11–3.13, security policy,
  contribution guide, and `docs/ENTERPRISE.md`.

### Deliberately not built

Phase 1's "do not build" list is binding: no sandboxes, async triggers, GitHub
App, PR automation, dashboard, or memory service. See Appendix A of the plan.

### Known gaps

Listed in the README and `docs/claude-code.md`. The significant one: identity
over MCP is process-scoped, so `agent_principal` is an attribution field rather
than an authentication result.
