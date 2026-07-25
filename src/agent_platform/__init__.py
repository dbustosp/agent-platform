"""Internal Agent Platform — the layer beneath any agent.

> The agent runtime is replaceable. The context and the evidence are ours.

Import discipline, enforced by review:

- `agent_platform.evidence`, `agent_platform.identity`, `agent_platform.policy`
  are the control plane. They may not import `deepagents`, `langchain`, or any
  cloud SDK at module scope. This is P1 made mechanical — point a second
  harness at the gateway and none of this moves.
- `agent_platform.gateway` may import the control plane and `mcp`. Never the
  harness.
- `agent_platform.harness` is the only package permitted to import
  `deepagents`. It is also the only package we expect to throw away.
"""

from agent_platform.evidence.record import EvidenceRecord, PolicyDecision
from agent_platform.identity import Principal, RunContext

__version__ = "0.1.0"

__all__ = [
    "EvidenceRecord",
    "Principal",
    "PolicyDecision",
    "RunContext",
    "__version__",
]
