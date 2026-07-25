"""Explicit model construction, and the `model_id` the evidence row needs.

`create_deep_agent(model=None)` is deprecated since deepagents 0.5.3 and
removed in 1.0.0, which §4.2 already notes. There is a second reason to be
explicit: Section 6 records `model_id` per tool call, and a model resolved
implicitly from the environment cannot be named in the record. A row that says
"some model did this" is not evidence.

Provider imports are lazy. `langchain-google-vertexai` is not installed and
`langchain-anthropic` requires a key to *use* — neither may be a condition of
importing this module, because the whole suite has to run with no credentials
and no network.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models import BaseChatModel

from agent_platform.harness.testing import ScriptedChatModel

Provider = Literal["fake", "anthropic", "vertex"]


class UnavailableModelProvider(RuntimeError):
    """Raised when a provider is requested but its package is not installed."""


def _require(provider: str, package: str, extra: str, exc: ImportError) -> UnavailableModelProvider:
    return UnavailableModelProvider(
        f"model provider {provider!r} needs {package}, which is not installed. "
        f"Install it with: pip install 'agent-platform[{extra}]'  ({exc})"
    )


def build_model(
    provider: Provider = "fake",
    *,
    model_name: str | None = None,
    scripted: ScriptedChatModel | None = None,
    **kwargs: Any,
) -> tuple[BaseChatModel, str]:
    """Return `(model, model_id)`.

    `model_id` is what lands in the evidence record, so it names the provider
    as well as the model: two providers can serve the same model name, and the
    record has to survive being read without this code next to it.

    No provider has a default `model_name`. Guessing one would put an
    unverified model string into an audit record, and a wrong `model_id` is
    worse than a missing agent.
    """
    if provider not in ("fake", "anthropic", "vertex"):
        msg = f"unknown model provider {provider!r}; expected one of: fake, anthropic, vertex"
        raise ValueError(msg)

    if provider == "fake":
        model = scripted if scripted is not None else ScriptedChatModel.from_script()
        return model, f"fake:{model_name or 'scripted'}"

    if not model_name:
        msg = f"model_name is required for provider {provider!r}; it is recorded as model_id"
        raise ValueError(msg)

    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:  # pragma: no cover - installed in this venv
            raise _require(provider, "langchain-anthropic", "harness", exc) from exc
        return ChatAnthropic(model_name=model_name, **kwargs), f"anthropic:{model_name}"

    # A2 makes Vertex the Phase 1 and Phase 3 model in both columns of §4.4 —
    # the package is simply not installed in this local checkout.
    try:
        from langchain_google_vertexai import ChatVertexAI
    except ImportError as exc:
        raise _require(provider, "langchain-google-vertexai", "gcp", exc) from exc
    return ChatVertexAI(model_name=model_name, **kwargs), f"vertex:{model_name}"
