"""`ScriptedChatModel` is what makes the rest of the suite runnable.

If these tests fail, every other harness test is untrustworthy, because they
all drive the agent through this model.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from agent_platform.harness.testing import ScriptedChatModel, tool_turn


def test_generic_fake_chat_model_still_cannot_bind_tools() -> None:
    """The reason `ScriptedChatModel` exists, asserted rather than assumed.

    If a future `langchain-core` implements `bind_tools` on the base class,
    this test fails and the subclass can be deleted. That is a better signal
    than the subclass quietly outliving its reason.
    """
    base = GenericFakeChatModel(messages=iter([AIMessage(content="hi")]))
    with pytest.raises(NotImplementedError):
        base.bind_tools([])


def test_bind_tools_returns_the_same_instance() -> None:
    """Returning `self` keeps one message iterator across the whole run.

    A `RunnableBinding` wrapper would work for a single turn and then desync
    the script the moment the agent looped.
    """
    model = ScriptedChatModel.from_script(final="done")
    assert model.bind_tools([{"name": "recall"}]) is model
    assert model.bind_tools([], tool_choice="auto") is model


def test_replays_tool_call_turns_then_the_final_answer() -> None:
    model = ScriptedChatModel.from_script(
        ("recall", {"subject": "billing"}),
        ("lookup_service_context", {"service": "billing"}),
        final="team-platform owns it.",
    )

    first = model.invoke("go")
    assert [c["name"] for c in first.tool_calls] == ["recall"]
    assert first.tool_calls[0]["args"] == {"subject": "billing"}

    second = model.invoke("go")
    assert [c["name"] for c in second.tool_calls] == ["lookup_service_context"]

    final = model.invoke("go")
    assert final.tool_calls == []
    assert final.content == "team-platform owns it."


def test_a_turn_can_request_parallel_tool_calls() -> None:
    """Parallel calls are the case most likely to break per-call accounting."""
    model = ScriptedChatModel.from_script(
        [("recall", {"subject": "a"}), ("recall", {"subject": "b"})],
        final="done",
    )
    turn = model.invoke("go")
    assert [c["args"]["subject"] for c in turn.tool_calls] == ["a", "b"]
    assert len({c["id"] for c in turn.tool_calls}) == 2


def test_ready_made_ai_messages_pass_through() -> None:
    scripted = tool_turn(("recall", {"subject": "x"}), text="thinking")
    model = ScriptedChatModel.from_script(scripted, final=None)
    turn = model.invoke("go")
    assert turn.content == "thinking"
    assert turn.tool_calls[0]["name"] == "recall"


def test_running_out_of_script_fails_loudly() -> None:
    """A bare `StopIteration` surfaces deep in LangGraph as something else."""
    model = ScriptedChatModel.from_script(final="done")
    model.invoke("go")
    with pytest.raises(AssertionError, match="exhausted after 1 turn"):
        model.invoke("go")
