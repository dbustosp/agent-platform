# The portability contract

Section 3.4 of the plan, restated here as the operational document. It is written
down so it survives staff turnover and schedule pressure — which is the only
reason a contract like this exists. Nobody violates it on purpose; they violate
it at 4pm on the Thursday before a demo, because the violating call is one
keyword shorter than the correct one.

## What the harness MAY own

Ephemeral state, per P4. If we lost it, could we recreate it by re-running? If
yes, the harness may own it.

- Conversation state and thread history
- Checkpoints
- Scratch files under `/tmp` and `/workspace`
- Tool-call retry logic
- Summarisation and compaction
- Prompt assembly

## What the harness MAY NOT own

Semantic state. Losing it means losing something we cannot regenerate.

- Memory as system of record
- Skills as system of record
- The evidence log
- Policy decisions
- Agent identity
- Credentials

## Banned by name

These fail silently and look identical to the correct choice. That is what makes
them worth naming rather than describing.

| Banned | Why | What we do instead |
|---|---|---|
| `ContextHubBackend` | Vendor-hosted memory. Same ergonomics as `StoreBackend`, opposite risk | `StoreBackend` with a store we constructed |
| `StoreBackend(store=None)` | Binds to whatever store the runtime supplies at call time | Always pass `store=` explicitly |
| Evidence written only to a tracing vendor | A trace is a copy, never the original | `EvidenceSink` writes to our store first; tracing is additive |
| Skills defined in framework config | Un-diffable, un-reviewable, un-portable | `SKILL.md` files in Git |
| Any hosted service in the runtime critical path | An open licence protects against code withdrawal, not service withdrawal | Local-first, cloud adapters behind an interface |

### `StoreBackend(store=None)` is not a hypothetical

Confirmed against `deepagents` 0.6.12. The signature really is:

```python
StoreBackend(runtime=None, *, store: BaseStore | None = None, namespace=None, file_format="v2")
```

and the docstring really does say the store is resolved via `get_store()` /
`get_runtime()` when `store` is `None`. Omitting one keyword argument silently
moves our memory into whatever store the runtime happens to supply. The code in
`agent_platform.harness.backends` therefore refuses to construct a
`StoreBackend` without an explicit store rather than trusting anyone to
remember.

## How this is enforced today

Phase 1 enforces the contract three ways, all of them in code rather than in
review:

1. **Import discipline.** `agent_platform.evidence`, `.identity`, and `.policy`
   do not import `deepagents`, `langchain`, or any cloud SDK. `agent_platform.gateway`
   does not import the harness. Only `agent_platform.harness` may touch the
   framework — and it is the only package we expect to delete.
2. **Structural guards.** `build_backend()` cannot produce a store-less
   `StoreBackend`. `build_agent()` refuses to assemble an agent whose middleware
   stack lacks `EvidenceMiddleware`.
3. **Enforcement location.** Policy is checked inside gateway dispatch, not in
   middleware. Deleting every middleware in the harness changes what is logged,
   not what is allowed.

## What is deliberately NOT enforced yet

Phase 1's "Do not build" list defers these. They are listed so their absence
reads as a decision:

- **The quarterly portability test (§7.2).** Deferred to Phase 3, where the plan
  puts it — "while it is still cheap to fix".
- **The 1,500 LOC middleware ceiling as an automated check (W2).** The ceiling
  applies now; the CI job that measures it does not exist yet.
  `EvidenceMiddleware` records its own line count in a module comment so the
  number is at least visible.
- **A banned-pattern linter.** The three structural guards above cover the two
  patterns that can actually occur in this codebase today.

None of these are hard to add. They are omitted because Phase 1 is scoped to
produce a number, and watch item W8 says the answer to scope creep is
"after the number".
