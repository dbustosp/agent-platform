# Claude Code as a second harness

This is Section 7.2's portability test, run early.

> Quarterly, half a day, one engineer: point a second harness at the gateway and
> run the eval suite. Not a migration — a smoke test.
> Report a single number to leadership: **harness switching cost, in
> engineer-weeks, verified [date].** Target: one sprint.

The plan defers this to Phase 3. It was run now because it is never going to be
cheaper, and because a portability claim nobody has tested is a slogan.

## Result

**Harness switching cost: well under one sprint. Verified 2026-07-25 against
Claude Code 2.1.215.**

What a second harness actually required:

| Layer | Work |
|---|---|
| Gateway tools | none — already an MCP server |
| Policy / AuthZ | none — enforced in gateway dispatch, so any client inherits it |
| Evidence for gateway tools | none — the gateway process writes those rows |
| Skills | none — `SKILL.md` is byte-compatible with Claude Code's own format |
| Evidence for the harness's own tools | **one module**, `agent_platform/claude_code/hook.py` |
| Schema changes | **none** |

That last row is the one that matters. P1's test is *"if it needs schema
changes, we leaked framework concepts into the store."* It needed none. A row
written by Claude Code and a row written by deepagents differ in exactly one
field — `harness_version` — which is the field that exists to record the
difference.

## What it looks like

One task, both paths, one evidence log:

```
Read                      allow  run=sess-portability-demo  parent=-                   claude-code==2.1.215
Bash                      allow  run=sess-portability-demo  parent=-                   claude-code==2.1.215
Grep                      allow  run=agent-explore-1        parent=sess-portability-demo  claude-code==2.1.215
get_control_requirements  allow  run=sess-portability-demo  parent=-                   deepagents==0.6.12
```

Subagent lineage falls out for free: Claude Code's `agent_id` is the delegate
and `session_id` is the parent, which is exactly Section 6's pair.

## Setup

```bash
./.venv/bin/python -m agent_platform.cli claude-code install
```

Writes three things and copies nothing:

- **`.mcp.json`** — registers the gateway as an MCP server. Claude Code will
  show it as *pending approval* until you approve it interactively; that is its
  trust prompt, and it is meant to be answered by a human.
- **`.claude/settings.json`** — registers the `PostToolUse` evidence hook.
- **`.claude/skills` → `skills/`** — a symlink, so Git stays the system of
  record (§3.3) rather than a copy in `.claude` drifting away from it.

Then restart Claude Code in the directory and check `/mcp`.

## Field mapping

| Section 6 | Claude Code | Note |
|---|---|---|
| `run_id` | `session_id`, or `agent_id` in a subagent | `session_id` already means "stable across a task" |
| `parent_run_id` | `session_id` when `agent_id` is present | |
| `agent_principal` | `AGENT_PRINCIPAL`, default `agent://claude-code/local` | |
| `delegated_by` | `DELEGATED_BY`, falling back to `$USER` | |
| `intent` | first user message in the transcript | "the task as stated", literally |
| `tool` | `tool_name` | |
| `args_hash` / `result_hash` | hashes of `tool_input` / `tool_response` | payloads never stored |
| `policy_decision` | always `allow` | see below |
| `harness_version` | `claude-code==<version>` from the transcript | a field, not a dependency |

### Why `policy_decision` is always `allow` here

`PostToolUse` fires only for tools that already ran, so any call this hook can
see is one the harness permitted. Denials issued by Claude Code's own permission
system never reach it. This is not a weakening of P2: the tools that carry a
real policy decision are the gateway's, and the gateway records those itself,
below any harness. A `PreToolUse` hook could observe Claude Code's own decisions
later if that ever becomes worth having.

## Design notes worth keeping

**The hook never fails loudly.** It runs inside somebody's editor loop. Every
error path logs and exits 0, and durability comes from `SpillingSink`
underneath. A hook that breaks the session gets deleted, and a deleted hook
loses every future row — a much worse outcome than one dropped record.

**It writes nothing to stdout or stderr.** Claude Code surfaces hook output to
the user on every tool call. The entrypoint is `-m agent_platform.claude_code`
rather than `-m agent_platform.claude_code.hook` specifically because the latter
makes CPython emit a `RuntimeWarning` the user would then see hundreds of times.

**Gateway calls are skipped, not duplicated.** The hook ignores
`mcp__agent-platform-gateway__*` because the gateway already recorded those with
the authoritative policy decision attached — the same de-duplication
`EvidenceMiddleware` performs with `GATEWAY_TOOL_NAMES`. Set
`GATEWAY_MCP_SERVER_NAME` if you register the server under a different name.

## Known gap: gateway rows do not join to the session

**This is the one finding of the portability test that is not already fixed.**

When *we* launch the gateway (`agent-platform run`), the run identity is passed
down through the environment and every row for a task shares a `run_id`.

When *Claude Code* launches the gateway from `.mcp.json`, it does not — it has
no knowledge of `AGENT_RUN_ID`, and `.mcp.json` carries only static environment.
So in a Claude Code session:

- hook rows carry the Claude Code `session_id`
- gateway rows carry a `run_id` the gateway process minted for itself

Both sets are internally consistent and neither is lost, but they cannot be
joined into a single task view.

Options, in increasing order of cost:

1. **Accept it.** Gateway rows are still complete, still policy-accurate, and
   still attributable to a principal and a time window.
2. **A `SessionStart` hook** writing the session id somewhere the gateway reads
   per call. Closes the gap; introduces a stateful side-channel that is racy
   when two sessions share a directory.
3. **Identity propagation in the protocol** — the caller asserts who it is on
   the MCP connection. This is the real answer, it is what Section 7.4's
   "agent identity as first-class" describes, and it belongs with Phase 4's
   machine identity rather than here.

Recommendation: option 1 now, option 3 as part of Phase 4. Option 2 buys a
partial fix at the price of a mechanism we would then have to remove.

## What this does *not* mean

Passing the portability test is not an argument for switching harness. Section
7.3 lists the conditions for moving, and requires **two or more** to hold:

- deployment/IAM burden of self-hosting LangGraph exceeds migration cost
- we need Java or Go agents
- platform-native audit emission materially reduces evidence-plumbing cost
- pre-1.0 breaking-change cadence becomes an operational tax

None currently hold. The value of this exercise is the number, not a migration:
we now know the exit is cheap, and we know it because we used it.
