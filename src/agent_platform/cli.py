"""The thin CLI — §5 Phase 1: "Deep Agents + LocalShellBackend + thin CLI".

Thin is the specification, not an apology. Everything here is composition:
read the environment, build the objects `config.py` knows how to build, hand
them to the layer that does the work. No policy, no schema, no knowledge, and
no evidence formatting decisions live in this file — those all belong to layers
that survive the harness.

Two commands do real work (`run`, `gateway serve`) and two exist so an operator
can see what the pilot has been doing without opening BigQuery (`evidence tail`,
`evidence stats`). The latter are not a dashboard: Appendix A defers a dashboard
until there is something worth displaying, and a `wc -l` on the evidence log is
how you find out whether there is.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

import typer

from agent_platform.config import (
    ConfigError,
    Settings,
    build_gateway,
    build_run_context,
    build_sink,
    load_dotenv,
)

app = typer.Typer(
    name="agent-platform",
    help="Governed context and evidence gateway for agents.",
    no_args_is_help=True,
    add_completion=False,
)
gateway_app = typer.Typer(name="gateway", help="Run the context gateway.", no_args_is_help=True)
evidence_app = typer.Typer(name="evidence", help="Inspect the evidence log.", no_args_is_help=True)
claude_code_app = typer.Typer(
    name="claude-code",
    help="Use Claude Code as a second harness against this gateway.",
    no_args_is_help=True,
)
app.add_typer(gateway_app)
app.add_typer(evidence_app)
app.add_typer(claude_code_app)


def _settings() -> Settings:
    load_dotenv()
    try:
        return Settings.from_env()
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


def _module_available(module: str) -> bool:
    """Is `module` importable?

    `find_spec` raises rather than returning None when an ancestor package is
    itself absent (`google.cloud.bigquery` with no `google` installed), so the
    obvious one-liner turns a missing optional extra into a crash. Doctor's
    entire job is to report on missing extras.
    """
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# ── run ──────────────────────────────────────────────────────────────────────


@app.command()
def run(
    intent: Annotated[str, typer.Argument(help="The task, as stated. Recorded verbatim.")],
    in_process: Annotated[
        bool,
        typer.Option(
            "--in-process/--mcp",
            help=(
                "Call the gateway directly instead of over an MCP connection. "
                "Same policy and evidence; skips the subprocess."
            ),
        ),
    ] = False,
    provider: Annotated[str | None, typer.Option(help="fake | anthropic | vertex")] = None,
    model_name: Annotated[str | None, typer.Option(help="Recorded as model_id.")] = None,
) -> None:
    """Run the agent on one task."""
    settings = _settings()
    chosen_provider = provider or settings.model_provider
    chosen_model = model_name or settings.model_name

    # Imported here, not at module scope: `gateway serve` and the evidence
    # commands must work on a machine with no harness installed (P5).
    try:
        from agent_platform.harness import HARNESS_VERSION, build_agent, build_backend, build_model
        from agent_platform.harness.gateway_tools import (
            agateway_tools_over_mcp,
            gateway_tools_in_process,
            stdio_connection,
        )
        from agent_platform.harness.skills_store import GitSkillStore
    except ImportError as exc:
        _err(f"the harness extra is not installed: {exc}\n  pip install -e '.[harness]'")
        raise typer.Exit(2) from exc

    if chosen_provider == "fake":
        _err(
            "provider 'fake' replays a scripted model and cannot do real work.\n"
            "  Set AGENT_MODEL_PROVIDER=anthropic (or vertex) and AGENT_MODEL_NAME,\n"
            "  or pass --provider/--model-name."
        )
        raise typer.Exit(2)

    try:
        model, model_id = build_model(chosen_provider, model_name=chosen_model)
    except (ValueError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc

    sink = build_sink(settings)
    context = build_run_context(
        settings, intent, model_id=model_id, harness_version=HARNESS_VERSION
    )

    async def _go() -> Any:
        if in_process:
            tools = gateway_tools_in_process(build_gateway(settings, context, sink))
        else:
            # Tools loaded over MCP are async-only — `StructuredTool` from the
            # adapter refuses sync invocation — so the whole run goes through
            # `ainvoke`. Sync tools still work on this path; the reverse is not
            # true, which is why there is no sync branch.
            #
            # `run_context=` is what keeps one task's calls under one run_id:
            # each sessionless tool call spawns its own gateway process.
            tools = await agateway_tools_over_mcp(stdio_connection(run_context=context))

        agent = build_agent(
            model=model,
            backend=build_backend(skill_store=GitSkillStore(settings.skills_dir)),
            tools=tools,
            sink=sink,
            run_context=context,
            run_limit=settings.run_limit,
        )
        typer.secho(f"run_id {context.run_id}", fg=typer.colors.CYAN, err=True)
        return await agent.ainvoke({"messages": [{"role": "user", "content": intent}]})

    try:
        result = asyncio.run(_go())
    finally:
        sink.close()

    messages = result.get("messages", []) if isinstance(result, dict) else []
    if messages:
        typer.echo(getattr(messages[-1], "content", ""))


# ── gateway ──────────────────────────────────────────────────────────────────


@gateway_app.command("serve")
def gateway_serve(
    intent: Annotated[
        str, typer.Option(help="Recorded as the intent for calls this server serves.")
    ] = "mcp gateway session",
) -> None:
    """Serve the gateway over stdio as an MCP server.

    This is the composition root the server module deliberately refuses to be:
    it reads the environment, so the evidence sink is a deployment decision that
    someone made rather than a default the gateway picked for itself.
    """
    settings = _settings()
    try:
        from agent_platform.gateway.server import serve_stdio
    except ImportError as exc:
        _err(f"the gateway extra is not installed: {exc}\n  pip install -e '.[gateway]'")
        raise typer.Exit(2) from exc

    sink = build_sink(settings)
    context = build_run_context(settings, intent)
    try:
        serve_stdio(build_gateway(settings, context, sink))
    finally:
        sink.close()


# ── evidence ─────────────────────────────────────────────────────────────────


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        _err(f"no evidence at {path}. Set EVIDENCE_PATH, or run the agent first.")
        raise typer.Exit(1)
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            _err(f"skipping malformed evidence line in {path}")
    return rows


@evidence_app.command("tail")
def evidence_tail(
    number: Annotated[int, typer.Option("-n", "--number", help="How many rows.")] = 20,
    path: Annotated[Path | None, typer.Option(help="Defaults to EVIDENCE_PATH.")] = None,
) -> None:
    """Print the most recent evidence rows."""
    settings = _settings()
    if settings.evidence_sink != "jsonl" and path is None:
        _err(f"tail reads JSONL; EVIDENCE_SINK is {settings.evidence_sink!r}. Pass --path.")
        raise typer.Exit(2)
    for row in _read_rows(path or settings.evidence_path)[-number:]:
        typer.echo(
            f"{row.get('timestamp', '?')}  {row.get('policy_decision', '?'):<8} "
            f"{row.get('tool', '?'):<26} {row.get('agent_principal', '?')} "
            f"<- {row.get('delegated_by', '?')}  {row.get('run_id', '?')}"
        )


@evidence_app.command("stats")
def evidence_stats(
    path: Annotated[Path | None, typer.Option(help="Defaults to EVIDENCE_PATH.")] = None,
) -> None:
    """Summarise the evidence log: rows, runs, tools, decisions."""
    settings = _settings()
    if settings.evidence_sink != "jsonl" and path is None:
        _err(f"stats reads JSONL; EVIDENCE_SINK is {settings.evidence_sink!r}. Pass --path.")
        raise typer.Exit(2)
    rows = _read_rows(path or settings.evidence_path)
    if not rows:
        typer.echo("no evidence rows yet")
        return

    tools = Counter(r.get("tool", "?") for r in rows)
    decisions = Counter(r.get("policy_decision", "?") for r in rows)
    runs = {r.get("run_id") for r in rows}
    stamps = sorted(r.get("timestamp", "") for r in rows if r.get("timestamp"))

    typer.echo(f"rows      {len(rows)}")
    typer.echo(f"runs      {len(runs)}")
    if stamps:
        typer.echo(f"first     {stamps[0]}")
        typer.echo(f"last      {stamps[-1]}")
    typer.echo("decisions " + ", ".join(f"{d}={n}" for d, n in sorted(decisions.items())))
    typer.echo("tools")
    for tool, n in tools.most_common():
        typer.echo(f"  {n:>6}  {tool}")


# ── claude-code (second harness, §7.2) ───────────────────────────────────────


@claude_code_app.command("hook")
def claude_code_hook() -> None:
    """PostToolUse hook: record one Claude Code tool call as evidence.

    Reads the hook payload on stdin. Always exits 0 — see the module docstring
    in `agent_platform.claude_code.hook` for why a hook that breaks the user's
    session is worse for the evidence log than a hook that drops a row.
    """
    from agent_platform.claude_code.hook import run_hook

    raise typer.Exit(run_hook())


@claude_code_app.command("install")
def claude_code_install(
    directory: Annotated[
        Path, typer.Option("--dir", help="Project to wire up. Defaults to the cwd.")
    ] = Path("."),
    force: Annotated[bool, typer.Option(help="Overwrite existing config.")] = False,
    pin_interpreter: Annotated[
        bool,
        typer.Option(
            "--pin-interpreter",
            help=(
                "Write this machine's absolute Python path instead of the console "
                "scripts. Not portable — do not commit the result."
            ),
        ),
    ] = False,
) -> None:
    """Point Claude Code at this gateway: MCP server, evidence hook, skills.

    Writes `.mcp.json` and `.claude/settings.json`, and links `skills/` into
    `.claude/skills/` so Git stays the system of record (§3.3) rather than a
    copy drifting inside `.claude`.
    """
    settings = _settings()
    root = directory.expanduser().resolve()
    if not root.is_dir():
        _err(f"not a directory: {root}")
        raise typer.Exit(2)

    python = sys.executable if pin_interpreter else None
    written: list[str] = []
    for path, payload in (
        (root / ".mcp.json", claude_code_mcp_config(python)),
        (root / ".claude" / "settings.json", claude_code_hook_config(python)),
    ):
        if path.exists() and not force:
            typer.secho(f"exists, skipping: {path} (use --force)", fg=typer.colors.YELLOW)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(str(path))

    link = root / ".claude" / "skills"
    source = settings.skills_dir.expanduser().resolve()
    if not source.is_dir():
        typer.secho(f"no skills directory at {source}", fg=typer.colors.YELLOW)
    elif link.exists() or link.is_symlink():
        typer.secho(f"exists, skipping: {link}", fg=typer.colors.YELLOW)
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        # Relative where possible: an absolute symlink into one developer's home
        # directory is broken in every other clone, and this file gets committed.
        try:
            target = Path(os.path.relpath(source, link.parent))
        except ValueError:  # different drive on Windows
            target = source
        link.symlink_to(target, target_is_directory=True)
        written.append(f"{link} -> {target}")

    for entry in written:
        typer.echo(f"wrote {entry}")
    typer.echo("\nRestart Claude Code in this directory, then check /mcp for the gateway.")


def claude_code_mcp_config(python: str | None = None) -> dict[str, Any]:
    """`.mcp.json` registering the gateway as an MCP server.

    Defaults to the `agent-platform` console script rather than an absolute
    interpreter path, because `.mcp.json` is a committed, shared file: an
    absolute path from one developer's machine is broken for everyone else.
    Pass `python` to pin a specific interpreter for a local or containerised
    deployment where PATH cannot be relied on.
    """
    if python:
        return {
            "mcpServers": {
                "agent-platform-gateway": {
                    "command": python,
                    "args": ["-m", "agent_platform.cli", "gateway", "serve"],
                }
            }
        }
    return {
        "mcpServers": {
            "agent-platform-gateway": {
                "command": "agent-platform",
                "args": ["gateway", "serve"],
            }
        }
    }


def claude_code_hook_config(python: str | None = None) -> dict[str, Any]:
    """`.claude/settings.json` registering the evidence hook.

    The matcher deliberately excludes nothing: the hook itself decides what to
    record, and it skips the gateway's own MCP tools because the gateway
    process already recorded those with the real policy decision attached.

    As with `.mcp.json`, the default command is the console script so the file
    is portable across clones.
    """
    # The package, not the submodule: see claude_code/__main__.py for why.
    command = f"{python} -m agent_platform.claude_code" if python else "agent-platform-hook"
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": command, "timeout": 30}],
                }
            ]
        }
    }


# ── doctor ───────────────────────────────────────────────────────────────────


@app.command()
def doctor() -> None:
    """Report what is wired, what is missing, and where evidence will land."""
    settings = _settings()
    typer.echo(f"principal    {settings.agent_principal}  <- {settings.delegated_by}")
    typer.echo(f"sink         {settings.evidence_sink} -> {settings.evidence_path}")
    typer.echo(f"spill        {settings.evidence_spill_path}")
    typer.echo(f"fail_closed  {settings.evidence_fail_closed}")
    typer.echo(f"model        {settings.model_provider}:{settings.model_name or '(unset)'}")
    typer.echo(f"skills       {settings.skills_dir}")
    typer.echo(f"tools        {', '.join(sorted(settings.allowed_tools))}")

    ok = True
    for label, module, extra in (
        ("gateway", "mcp", "gateway"),
        ("harness", "deepagents", "harness"),
        ("bigquery", "google.cloud.bigquery", "gcp"),
        ("vertex", "langchain_google_vertexai", "gcp"),
    ):
        found = _module_available(module)
        mark = "ok     " if found else "missing"
        typer.echo(f"{mark}      {label:<10} ({module}, extra: {extra})")
        if not found and label in {"gateway", "harness"}:
            ok = False

    try:
        knowledge = settings.knowledge_dir
        from agent_platform.config import build_knowledge

        source = build_knowledge(settings)
        typer.echo(
            f"knowledge    {knowledge or '(default)'}: "
            f"{len(source.known_services())} services, "
            f"{len(source.known_change_classes())} change classes"
        )
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        typer.secho(f"knowledge    FAILED: {exc}", fg=typer.colors.YELLOW)

    if not settings.skills_dir.is_dir():
        typer.secho(f"skills       MISSING: {settings.skills_dir}", fg=typer.colors.YELLOW)

    raise typer.Exit(0 if ok else 1)


def main() -> None:  # pragma: no cover - console-script shim
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app() or 0)
