"""The provider preset catalog.

A preset is a *starting point*: a base URL, a default protocol, where the
model list lives, and a note about what the provider does. It is not
configuration and it is not business logic; it never reaches the highlight
service.

Three rules shape this catalog.

**Stable defaults only.** A preset says where a provider lives and how to
talk to it. It does not carry a list of models, a price table, or a claim
that a model is free. Those change without notice, and a stale copy in the
source would be worse than no copy at all.

**A protocol is a property of a model, and sometimes of a provider.** Some
providers speak more than one API. OpenCode Zen is the case in point: its
own documentation lists models against an OpenAI *Responses* endpoint, an
Anthropic *Messages* endpoint, an OpenAI *Chat Completions* endpoint and a
Google endpoint, depending on the model. A preset may carry a *routing
hint* -- which API a family of models speaks -- but a hint is never a
promise, and the user can always override it. A model the hint does not
cover is reported as having an unknown protocol, and is never routed to an
incompatible endpoint.

**Free is not a property this application can assert.** A provider may
offer a free tier, a limited-time free model, or a trial that expires.
Those are different things, and they change. The catalog therefore records
a *tier* and a plain-language note, and the user-facing surface says that
pricing and availability are the provider's to decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from app.integrations import ApiProtocol


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """A provider's stable defaults."""

    id: str
    name: str
    summary: str
    #: The protocol a new configuration of this provider starts with.
    default_protocol: ApiProtocol
    #: The base URL to use for each protocol, when the provider serves
    #: more than one API.
    base_urls: Mapping[ApiProtocol, str]
    #: Where the model list lives, when the provider publishes one.
    models_url: str | None = None
    #: Whether the provider can be asked for its models at all.
    supports_model_discovery: bool = False
    #: The kind of free access the provider documents, if any.
    free_tier: str = "unknown"
    #: A plain-language note about cost, shown next to the configuration.
    free_tier_note: str = (
        "Ask the provider what this model costs. Prices and free access "
        "change, and ClipForge does not track them."
    )
    #: Routing hints: a model-id prefix and the protocol its family speaks.
    #: A hint, not an inventory: the user can override it.
    protocol_hints: tuple[tuple[str, ApiProtocol], ...] = ()
    #: Where the provider documents itself.
    docs_url: str = ""

    def base_url_for(self, protocol: ApiProtocol) -> str | None:
        """The default base URL for a protocol, when there is one."""
        return self.base_urls.get(protocol)

    def protocol_for_model(self, model_id: str) -> ApiProtocol | None:
        """The protocol a model speaks, when the preset can say.

        The longest matching prefix wins, so a more specific hint beats a
        more general one. ``None`` means *not known*, which the caller must
        not turn into a guess.
        """
        best: tuple[int, ApiProtocol] | None = None
        for prefix, protocol in self.protocol_hints:
            if model_id.startswith(prefix) and (
                best is None or len(prefix) > best[0]
            ):
                best = (len(prefix), protocol)
        return best[1] if best is not None else None


#: The provider every configuration can be built from.
CUSTOM_PROVIDER = ProviderPreset(
    id="custom",
    name="Custom",
    summary="Any endpoint that speaks one of the supported protocols.",
    default_protocol=ApiProtocol.OPENAI_CHAT_COMPLETIONS,
    base_urls={},
    models_url=None,
    supports_model_discovery=True,
    free_tier="unknown",
    free_tier_note=(
        "Ask the provider what this model costs. Prices and free access "
        "change, and ClipForge does not track them."
    ),
    docs_url="",
)


#: OpenCode Zen -- a pay-as-you-go gateway that speaks several APIs.
OPENCODE_ZEN = ProviderPreset(
    id="opencode_zen",
    name="OpenCode Zen",
    summary="OpenCode's model gateway. Which API a model speaks depends on the model.",
    default_protocol=ApiProtocol.OPENAI_CHAT_COMPLETIONS,
    base_urls={
        ApiProtocol.OPENAI_CHAT_COMPLETIONS: "https://opencode.ai/inference/openai/v1",
        ApiProtocol.OPENAI_RESPONSES: "https://opencode.ai/inference/openai/v1",
        ApiProtocol.ANTHROPIC_MESSAGES: "https://opencode.ai/inference/anthropic/v1",
    },
    # The gateway publishes its model list under /zen/v1, which is a
    # different prefix from the inference endpoints.
    models_url="https://opencode.ai/zen/v1/models",
    supports_model_discovery=True,
    free_tier="trial",
    free_tier_note=(
        "OpenCode publishes some models as free for a limited time and lists "
        "prices for the rest. Both are the provider's to change. Check "
        "their pricing page before you rely on it."
    ),
    protocol_hints=(
        ("gpt-", ApiProtocol.OPENAI_RESPONSES),
        ("grok-", ApiProtocol.OPENAI_RESPONSES),
        ("muse-", ApiProtocol.OPENAI_RESPONSES),
        ("claude-", ApiProtocol.ANTHROPIC_MESSAGES),
        ("deepseek-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("minimax-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("glm-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("kimi-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("mistral-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("big-pickle", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("beam-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("ll-alpha-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("longcat-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("exo-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("mimo-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
        ("nemotron-", ApiProtocol.OPENAI_CHAT_COMPLETIONS),
    ),
    docs_url="https://opencode.ai/docs/zen/",
)


#: Groq -- OpenAI-compatible, with a model list and a documented free tier.
GROQ = ProviderPreset(
    id="groq",
    name="Groq",
    summary="Fast inference on an OpenAI-compatible API, with a free tier.",
    default_protocol=ApiProtocol.OPENAI_CHAT_COMPLETIONS,
    base_urls={
        ApiProtocol.OPENAI_CHAT_COMPLETIONS: "https://api.groq.com/openai/v1",
        ApiProtocol.OPENAI_RESPONSES: "https://api.groq.com/openai/v1",
    },
    models_url="https://api.groq.com/openai/v1/models",
    supports_model_discovery=True,
    free_tier="trial",
    free_tier_note=(
        "Groq's free tier has per-model request and token limits that are "
        "lower than its paid plans. The limits and the prices are the "
        "provider's to change."
    ),
    docs_url="https://console.groq.com/docs/overview",
)


#: Cerebras -- OpenAI-compatible, with a time- and credit-bounded trial.
CEREBRAS = ProviderPreset(
    id="cerebras",
    name="Cerebras",
    summary="Fast inference on an OpenAI-compatible API.",
    default_protocol=ApiProtocol.OPENAI_CHAT_COMPLETIONS,
    base_urls={
        ApiProtocol.OPENAI_CHAT_COMPLETIONS: "https://api.cerebras.ai/v1",
    },
    models_url="https://api.cerebras.ai/v1/models",
    supports_model_discovery=True,
    free_tier="trial",
    free_tier_note=(
        "Cerebras offers new accounts a credit allowance that expires after "
        "a limited time; their documentation states there is no permanently "
        "free tier. Check their pricing page for the current terms."
    ),
    docs_url="https://inference-docs.cerebras.ai/",
)


#: Every preset, in the order they are offered.
PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    OPENCODE_ZEN,
    GROQ,
    CEREBRAS,
    CUSTOM_PROVIDER,
)

_PRESETS_BY_ID: dict[str, ProviderPreset] = {
    preset.id: preset for preset in PROVIDER_PRESETS
}


def get_preset(provider_id: str) -> ProviderPreset | None:
    """The preset with this id, when there is one."""
    return _PRESETS_BY_ID.get(provider_id)


def preset_ids() -> tuple[str, ...]:
    """The ids of every preset."""
    return tuple(preset.id for preset in PROVIDER_PRESETS)
