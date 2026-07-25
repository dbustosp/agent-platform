## What and why

<!-- What changes, and which plan section or principle it serves (§4.1, P2, W2). -->

## Verification

<!-- Paste real output. "Tests pass" without counts will be sent back. -->

```
$ python -m pytest -q

$ python -m ruff check src tests
```

## Checklist

- [ ] Tests added or updated, and mutation-checked if this fixes a bug
      (revert the fix → the new test fails → restore)
- [ ] No new dependency in `evidence/`, `identity`, `policy`, `config`,
      `gateway`, or `claude_code` on `deepagents` / `langchain` / a cloud SDK (P1)
- [ ] Policy still enforced at the tool boundary, not in middleware or a prompt (P2)
- [ ] No §3.4 banned construction introduced
- [ ] Suite still runs with no credentials and no network
- [ ] Docs updated if behaviour or a documented gap changed
