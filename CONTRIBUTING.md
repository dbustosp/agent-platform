# Contributing

## Setup

```bash
python3.13 -m venv .venv
./.venv/bin/python -m pip install -e '.[gateway,harness,dev]'
./.venv/bin/python -m pytest
```

Python 3.11, 3.12 and 3.13 are supported and all three run in CI. Nothing needs
credentials or network access — if a change makes the suite require either, that
is the bug.

## Before you open a PR

```bash
./.venv/bin/python -m ruff check src tests
./.venv/bin/python -m ruff format src tests
./.venv/bin/python -m pytest
```

Or install the hooks and let them run it: `pre-commit install`.

## The rules that are not style preferences

This repository implements `docs/PLAN.md`, and a few of its constraints are
load-bearing. A PR that breaks one of these will fail CI, but it is faster to
know why up front.

**Import discipline (P1).** `agent_platform.evidence`, `.identity`, `.policy`,
`.config` and `.gateway` may not import `deepagents`, `langchain`, or any cloud
SDK. Only `agent_platform.harness` may. `agent_platform.claude_code` may import
neither. CI installs the project *without* the harness extra and asserts the
control plane still imports.

**Enforce at the tool boundary (P2).** Policy decisions belong in gateway
dispatch. If a control can be removed by deleting middleware or editing a
prompt, it is not a control. Adding a check in middleware "as well" is fine;
adding one *instead* is not.

**Evidence at the point of action (P3).** One row per tool call, authored by
whichever component holds the authoritative policy decision. Never reconstruct
records from traces, and never make a tracing vendor the only writer.

**The §3.4 banned list.** No `ContextHubBackend`. No `StoreBackend` without an
explicit `store=`. No skills defined in framework config instead of Git files.
CI greps for the first two.

**Middleware budget (W2).** Custom `AgentMiddleware` is capped at 1,500 lines
across the project. Past that, the logic belongs in an MCP server. CI measures
it and fails the build.

## Tests

Two things get a PR sent back faster than anything else:

**A test that cannot fail.** Asserting that a denied tool "returns a denial"
without proving the tool body never ran is not a test of the control. Prove the
side effect did not happen.

**A claim that the suite passes without having run it.** Paste real counts.

New tests for a bug fix should be mutation-checked: revert the fix, confirm the
test fails, restore it. Say so in the PR description.

## Commits and PRs

Conventional-commit prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
Keep the subject under ~72 characters and explain *why* in the body — the diff
already shows what.

Reference the plan section a change implements or changes (`§4.1`, `P2`, `W2`).
Much of this code exists because of a specific paragraph, and losing that link
is how the constraints erode.
