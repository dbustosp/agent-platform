"""Claude Code as a second harness — the §7.2 portability test, made real.

> *Test:* point a second harness at the gateway and run the eval suite. If it
> needs schema changes, we leaked framework concepts into the store. — P1

Nothing in this package imports `deepagents` or `langchain`. That is the whole
point: if the control plane were entangled with the first harness, a second one
could not reuse it, and everything in Sections 3 and 7 would be aspiration.

What it actually took to add a second harness:

| Layer | Work |
|---|---|
| Gateway tools | none — it is already an MCP server |
| Policy | none — enforced in gateway dispatch, so any client inherits it |
| Evidence for gateway tools | none — the gateway process writes those rows |
| Skills | none — `SKILL.md` is Claude Code's native format |
| Evidence for the harness's own tools | this package |

`hook.py` is the counterpart of `harness/middleware.py`: same job, same records,
same sink, different harness. It is the only thing that had to be written.
"""

from agent_platform.claude_code.hook import (
    CLAUDE_CODE_PRINCIPAL,
    record_from_payload,
    run_hook,
)

__all__ = ["CLAUDE_CODE_PRINCIPAL", "record_from_payload", "run_hook"]
