# Verified API facts — `deepagents` 0.6.12 / `langchain` 1.3.14

Everything below was read out of the installed wheel in `.venv` (Python 3.13.3)
on 2026-07-25, not from documentation or memory. Section 4 of `PLAN.md` was
checked against it line by line and found accurate.

## Installed versions

| Package | Version |
|---|---|
| `deepagents` | 0.6.12 (`deepagents.__version__ == "0.6.12"`) |
| `langchain` | 1.3.14 |
| `langchain-core` | 1.5.1 |
| `langgraph` | 1.2.9 |
| `mcp` | 1.28.1 |
| `langchain-mcp-adapters` | 0.3.0 |

## `deepagents.backends`

```python
CompositeBackend(default: BackendProtocol | StateBackend,
                 routes: dict[str, BackendProtocol],
                 *, artifacts_root: str = "/")

StoreBackend(runtime: object = None, *, store: BaseStore | None = None,
             namespace: NamespaceFactory | None = None,
             file_format: FileFormat = "v2")

LocalShellBackend(root_dir: str | Path | None = None, *,
                  virtual_mode: bool | None = None,
                  timeout: int = DEFAULT_EXECUTE_TIMEOUT,
                  max_output_bytes: int = 100_000,
                  env: dict[str, str] | None = None,
                  inherit_env: bool = False)

ContextHubBackend(identifier: str, *, client: Client | None = None)   # BANNED (§3.4)
```

`NamespaceFactory = Callable[[Runtime[Any]], tuple[str, ...]]`

**§4.1's warning is real.** `StoreBackend.__init__` defaults `store=None`, and the
docstring confirms the store is then resolved via `get_store()` / `get_runtime()`
at call time. Passing `store=` explicitly is the difference between owning the
store and inheriting whatever the runtime supplies.

## `deepagents.create_deep_agent`

Every keyword used in §4.2 exists:

```python
create_deep_agent(
    model: str | BaseChatModel | None = None,
    tools: Sequence[BaseTool | Callable | dict] | None = None,
    *,
    system_prompt: str | SystemMessage | None = None,
    middleware: Sequence[AgentMiddleware] = (),
    subagents: ... = None,
    skills: list[str] | None = None,
    memory: list[str] | None = None,
    permissions: list[FilesystemPermission] | None = None,
    backend: BackendProtocol | BackendFactory | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    response_format=None, state_schema=None, context_schema=None,
    checkpointer=None, store: BaseStore | None = None,
    debug: bool = False, name: str | None = None, cache=None,
)
```

`model=None` is deprecated since 0.5.3 and removed in 1.0.0 — §4.2's "explicit;
the default is deprecated" comment is correct.

## Middleware

`ToolErrorMiddleware` and `ModelCallLimitMiddleware` come from
`langchain.agents.middleware`, **not** from `deepagents`:

```python
from langchain.agents.middleware import (
    AgentMiddleware, ToolErrorMiddleware, ModelCallLimitMiddleware,
    InterruptOnConfig, ToolCallRequest,
)

ToolErrorMiddleware(on_error=None, *, aon_error=None, tools=None)
ModelCallLimitMiddleware(*, thread_limit=None, run_limit=None,
                         exit_behavior: Literal["end", "error"] = "end")
```

### The hook `EvidenceMiddleware` uses

```python
class AgentMiddleware:
    def wrap_tool_call(self, request: ToolCallRequest,
                       handler: Callable[[ToolCallRequest], ToolMessage | Command]
                       ) -> ToolMessage | Command: ...
    async def awrap_tool_call(self, request, handler): ...
```

`ToolCallRequest` is a dataclass with `.tool_call` (a `ToolCall` dict carrying
`name`, `args`, `id`), `.tool`, `.state`, `.runtime`. Direct attribute
assignment is deprecated — use `request.override(**kwargs)`.

This hook fires **once per tool call, around execution**, which is exactly what
P3 requires: the record is emitted at the point of action rather than
reconstructed afterwards.

## `FilesystemPermission`

```python
@dataclass
class FilesystemPermission:
    operations: list[FilesystemOperation]
    paths: list[str]                    # must start with "/"
    mode: Literal["allow", "deny", "interrupt"] = "allow"
```

## Skills

`SkillsMiddleware` expects each skill to be **a directory containing a
`SKILL.md`** with YAML frontmatter (`name`, `description`) followed by markdown
instructions. Progressive disclosure: the frontmatter is injected into the
system prompt; the body is read on demand via `read_file`.

## Testing without credentials — verified working

`GenericFakeChatModel` raises `NotImplementedError` from `bind_tools`, so it
cannot drive an agent as-is. Subclassing it to accept the binding works:

```python
class ScriptedChatModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self
```

A full `create_deep_agent(...).invoke(...)` round trip was executed with this
model, a scripted `AIMessage(tool_calls=[...])`, and a `wrap_tool_call`
middleware. The tool ran and the middleware captured exactly one record. No API
key, no network.
