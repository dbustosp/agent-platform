"""Context Gateway — the single interface between any agent and our knowledge.

§2.1 marks this "Build", and §3.2 puts it above the line we own forever. It may
import the control plane (`evidence`, `identity`, `policy`) and the `mcp`
package. It may never import `agent_platform.harness`, `deepagents` or
`langchain`: the gateway has to outlive the harness pointed at it, and an
import is how that stops being true.

`server.py` is not re-exported here. MCP is an optional extra (`pip install
agent-platform[gateway]`), and the tools must stay importable — and testable —
by a process that has no MCP at all. Import `agent_platform.gateway.server`
directly when you want the transport.
"""

from agent_platform.gateway.knowledge import (
    Assertion,
    Control,
    ControlRequirements,
    KnowledgeError,
    KnowledgeSource,
    ServiceContext,
    YamlKnowledgeSource,
    default_knowledge_dir,
    load_sample_knowledge,
)
from agent_platform.gateway.tools import (
    GATEWAY_TOOL_NAMES,
    READ_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    Gateway,
    UnknownGatewayTool,
    read_only_policy,
)

__all__ = [
    "GATEWAY_TOOL_NAMES",
    "READ_TOOL_NAMES",
    "WRITE_TOOL_NAMES",
    "Assertion",
    "Control",
    "ControlRequirements",
    "Gateway",
    "KnowledgeError",
    "KnowledgeSource",
    "ServiceContext",
    "UnknownGatewayTool",
    "YamlKnowledgeSource",
    "default_knowledge_dir",
    "load_sample_knowledge",
    "read_only_policy",
]
