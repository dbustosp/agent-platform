"""`python -m agent_platform.claude_code` — the PostToolUse hook entrypoint.

A dedicated `__main__` rather than `python -m agent_platform.claude_code.hook`:
running a submodule that its own package has already imported makes CPython emit
a `RuntimeWarning` on stderr. Claude Code shows hook stderr to the user, so that
warning would appear on every single tool call.
"""

from __future__ import annotations

import sys

from agent_platform.claude_code.hook import main

if __name__ == "__main__":
    sys.exit(main())
