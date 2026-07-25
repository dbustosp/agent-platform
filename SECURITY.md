# Security policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](../../security/advisories/new) rather than opening
a public issue.

Include what you did, what happened, and what you expected. A proof of concept
helps but is not required to file. You should get an acknowledgement within a
few working days.

Please do not test against systems you do not own. Everything in this
repository runs locally with no credentials, so a local reproduction is almost
always possible.

## Supported versions

This is pre-1.0 and pinned to a specific harness version (see §7.1 of
`docs/PLAN.md`). Security fixes land on `main`. There is no backport branch.

## Scope

In scope:

- The evidence log producing an incomplete, forgeable, or silently-dropped record
- Policy enforcement bypass at the gateway tool boundary
- Secrets or payload data leaking into evidence records, logs, or spill files
- Path traversal or arbitrary read through gateway tools or the skills store
- The Claude Code hook crashing, hanging, or leaking data from a session

Out of scope:

- Vulnerabilities in `deepagents`, `langchain`, or the MCP SDK — report those
  upstream, then open an issue here so we can pin around them
- Prompt injection causing a model to *call* an allowed tool. That is the threat
  model, not a bypass: the gateway assumes the model is untrusted and enforces
  at the tool boundary regardless of what the prompt says (P2). A report is only
  in scope if it shows a call succeeding that policy should have denied.

## Security properties this project asserts

These are the claims worth attacking. Each is enforced in code and covered by a
test, not asserted in a doc.

| Property | Where |
|---|---|
| Policy is evaluated before any tool body runs, inside gateway dispatch | `gateway/tools.py`, `tests/test_gateway_policy.py` |
| Default-deny: an unlisted tool is refused | `policy.py::AllowlistPolicy` |
| A denied call is still recorded | `tests/test_gateway_policy.py` |
| Tool payloads are hashed, never stored | `evidence/hashing.py`, `tests/test_hashing.py` |
| A sink outage spills to disk rather than dropping a record | `evidence/sink.py::SpillingSink` |
| The skills store is read-only to the agent | `harness/skills_store.py` |
| Removing harness middleware changes what is logged, never what is allowed | `docs/portability-contract.md` |

## Known limitations

Documented rather than hidden. See the *Known Phase-1 gaps* section of the
README and `docs/claude-code.md`. The one with security relevance:

- **Identity over MCP stdio is process-scoped.** The gateway records the
  principal from its own environment; the transport carries no caller identity.
  A gateway launched by an untrusted process records that process's claimed
  identity. Do not treat `agent_principal` as authenticated in Phase 1 —
  it is an attribution field, not an authentication result. Phase 4's machine
  identity work is what changes that.
