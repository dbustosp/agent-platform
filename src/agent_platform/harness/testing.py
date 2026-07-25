"""A chat model that replays a script, so the suite needs no API key.

Every test in this repository must pass with no credentials and no network.
That is not a convenience: a pilot whose tests only run for people with a
Vertex project is a pilot nobody else can maintain, and the harness is the
layer we most expect somebody else to have to maintain (Section 7.1).

`GenericFakeChatModel` almost does the job but raises `NotImplementedError`
from `bind_tools`, so an agent cannot bind its tool schemas to it. Overriding
`bind_tools` to accept the binding and return `self` is enough — the fake never
looks at the tools, because the tool calls it emits are the ones the script
says it emits. Verified against `langchain-core` 1.5.1 in `VERIFIED-API.md`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

#: One requested tool call: `(tool_name, args)`.
ToolCallSpec = tuple[str, Mapping[str, Any]]


def tool_turn(*calls: ToolCallSpec, text: str = "") -> AIMessage:
    """Build one assistant turn that requests `calls`.

    Multiple specs in a single turn become parallel tool calls, which is the
    case most likely to break per-tool-call accounting in `EvidenceMiddleware`
    and therefore the case worth being able to script.
    """
    return AIMessage(
        content=text,
        tool_calls=[
            {"name": name, "args": dict(args), "id": f"call_{uuid.uuid4().hex[:12]}"}
            for name, args in calls
        ],
    )


def _replay(messages: Sequence[AIMessage]) -> Iterator[AIMessage]:
    """Yield the script, then fail loudly rather than silently.

    A bare `StopIteration` here surfaces deep inside LangGraph as something
    unrelated. Running out of script means the agent asked for more model
    calls than the test expected, which is usually the finding.
    """
    yield from messages
    msg = (
        f"ScriptedChatModel exhausted after {len(messages)} turn(s): the agent "
        "requested another model call. Add a turn to the script or check why "
        "the loop did not terminate."
    )
    raise AssertionError(msg)


class ScriptedChatModel(GenericFakeChatModel):
    """A `GenericFakeChatModel` that an agent can actually bind tools to."""

    def bind_tools(self, tools: Iterable[Any], **kwargs: Any) -> ScriptedChatModel:
        """Accept the tool binding and ignore it.

        The script decides what the model "calls"; the bound schemas are
        irrelevant to a replay. Returning `self` rather than a `RunnableBinding`
        keeps the message iterator shared across the whole agent run, which is
        what makes a multi-turn script work.
        """
        return self

    @classmethod
    def from_script(
        cls,
        *turns: AIMessage | ToolCallSpec | Sequence[ToolCallSpec],
        final: str | None = "Done.",
    ) -> ScriptedChatModel:
        """Script a sequence of tool-call turns followed by a final answer.

        Each turn may be a ready-made `AIMessage`, a single `(name, args)`
        pair, or a sequence of pairs for parallel calls. `final=None` omits the
        closing text turn, for tests that want the script to run out.
        """
        messages: list[AIMessage] = []
        for turn in turns:
            if isinstance(turn, AIMessage):
                messages.append(turn)
            elif isinstance(turn, tuple) and len(turn) == 2 and isinstance(turn[0], str):
                messages.append(tool_turn(turn))  # type: ignore[arg-type]
            else:
                messages.append(tool_turn(*turn))  # type: ignore[misc]
        if final is not None:
            messages.append(AIMessage(content=final))
        return cls(messages=_replay(messages))
