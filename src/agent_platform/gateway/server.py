"""MCP server over the gateway tools.

Tools, not resources: §3.3 says so explicitly, because major clients — Copilot's
cloud agent among them — support MCP tools only. A resource-shaped gateway would
be unreachable from half the harnesses we expect to point at it.

**Why `record_evidence` is not here.** §3.3 lists four tools; this server
registers three. The plan resolves its own apparent contradiction in the same
table row: `record_evidence` is "called by the harness middleware", and §5
Phase 1 specifies "one MCP connection to the gateway, read-only". The middleware
runs in the harness process and calls `Gateway.record_evidence` as a Python
method; it never needs the MCP surface. So the write tool exists on the
`Gateway` object, is covered by the same policy check, and is simply not
advertised to the model. An agent that cannot write evidence cannot shape the
record of what it did — which is the whole point of keeping the log ours.

The MCP layer is a thin adapter and nothing else. Policy, evidence and knowledge
all live below it, so a second transport (HTTP in Phase 3, or whatever replaces
MCP) is another adapter rather than another copy of the controls.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from agent_platform.gateway.tools import Gateway

DEFAULT_SERVER_NAME = "agent-platform-gateway"

SERVER_INSTRUCTIONS = """\
Governed access to internal engineering knowledge.

Use lookup_service_context before proposing a change to a service you have not
been told about: it returns the owning team, the service's dependencies and its
criticality. Use get_control_requirements to find the controls that apply to a
class of change before describing how to make it. Use recall to retrieve prior
assertions about a subject rather than assuming.

Every call is authorised and logged. A refusal is a real answer: report it,
do not work around it.
"""


def create_gateway_server(
    gateway: Gateway,
    *,
    name: str = DEFAULT_SERVER_NAME,
    instructions: str = SERVER_INSTRUCTIONS,
) -> FastMCP:
    """Build a configured MCP server exposing the three read tools.

    The `gateway` is captured by the tool closures, so the acting principal and
    run context are fixed by whoever constructed the server. Nothing arriving
    over the wire can change them.
    """

    server: FastMCP = FastMCP(name=name, instructions=instructions)

    @server.tool(
        name="lookup_service_context",
        description=(
            "Ownership, dependencies and criticality for an internal service or repo. "
            "Returns found=false when we hold no context for the name given."
        ),
    )
    def lookup_service_context(service: str) -> dict[str, Any]:
        """Look up who owns a service, what it depends on, and how critical it is."""
        return gateway.lookup_service_context(service)

    @server.tool(
        name="get_control_requirements",
        description=(
            "Controls and standards that apply to a class of change "
            "(for example 'schema-migration' or 'iam-policy-change'). "
            "Returns found=false for an unrecognised change class."
        ),
    )
    def get_control_requirements(change_class: str) -> dict[str, Any]:
        """List the applicable controls for a class of change."""
        return gateway.get_control_requirements(change_class)

    @server.tool(
        name="recall",
        description=(
            "Prior assertions recorded about a subject — a service, a system or a "
            "decision — each with the source it came from. Returns found=false and an "
            "empty list when nothing has been recorded."
        ),
    )
    def recall(subject: str) -> dict[str, Any]:
        """Retrieve what we already know about a subject."""
        return gateway.recall(subject)

    return server


def serve_stdio(
    gateway: Gateway,
    *,
    name: str = DEFAULT_SERVER_NAME,
) -> None:
    """Run the gateway over stdio until the client disconnects.

    This is the entrypoint the CLI launches. It blocks, and it owns no state:
    kill it and the evidence already written is unaffected (P5).
    """
    create_gateway_server(gateway, name=name).run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - deployment guard, not logic
    # Deliberately not a working `python -m` entrypoint. Choosing an evidence
    # sink is a deployment decision — dev JSONL, BigQuery dev dataset, governed
    # dataset — and a module that picks one for you is a module that can start
    # the gateway with its evidence going somewhere nobody chose.
    raise SystemExit(
        "agent_platform.gateway.server is not directly runnable: the gateway "
        "requires an evidence sink it must not choose for itself. Launch it with "
        "the agent-platform CLI, or call serve_stdio(gateway) from your own process."
    )
