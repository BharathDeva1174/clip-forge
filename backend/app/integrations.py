"""The integration domain model.

An *integration* is a saved, named configuration for one AI provider: where
it lives, how to talk to it, which model to use, and which secret to reach
for. It is the only thing the highlight service knows about, and it holds
no provider-specific behaviour: the protocol decides the wire format, the
provider preset only supplies defaults.

Nothing in this module performs I/O, so the rules can be applied anywhere.

The important distinction this module draws is between **model metadata**
and **universal parameters**. The context window, the maximum output tokens,
the name of the reasoning-effort parameter and the values it accepts, and
the structured-output mode are all properties of a *model*. They are not
things every API has, and sending one to a model that does not understand
it is how a request that would otherwise work gets rejected. Unknown
metadata is therefore represented as unknown, never as a default that is
sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ApiProtocol(StrEnum):
    """The wire protocols an integration can speak.

    A configuration names its protocol explicitly. A provider preset may
    choose a sensible default, but the protocol is always the
    configuration's own property, because the same provider often serves
    more than one protocol and the user may need to match it.
    """

    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"

    #: Recognised, but not implemented by ClipForge. A configuration that
    #: needs one of these is reported as unsupported rather than routed to
    #: an endpoint that cannot understand it.
    GOOGLE_GENERATE_CONTENT = "google_generate_content"
    SYSTEM_ONE = "system_one"


#: The protocols this build can actually speak.
IMPLEMENTED_PROTOCOLS: frozenset[ApiProtocol] = frozenset(
    {
        ApiProtocol.OPENAI_CHAT_COMPLETIONS,
        ApiProtocol.OPENAI_RESPONSES,
        ApiProtocol.ANTHROPIC_MESSAGES,
    }
)

#: Protocols that are recognised but not implemented.
UNSUPPORTED_PROTOCOLS: frozenset[ApiProtocol] = frozenset(
    set(ApiProtocol) - IMPLEMENTED_PROTOCOLS
)


class StructuredOutputMode(StrEnum):
    """How a model can be asked for structured output.

    ``NONE`` means the model has no structured-output support, so the
    prompt asks for JSON and the answer is validated locally.
    """

    NONE = "none"
    JSON_OBJECT = "json_object"
    JSON_SCHEMA = "json_schema"


class PricingStatus(StrEnum):
    """What is known about the cost of a configuration.

    The default is ``UNKNOWN``, and it stays unknown unless the user says
    otherwise or a provider actually tells us. Pricing and availability are
    controlled by the provider and change without notice, so the
    application never asserts that a provider or a model is free.
    """

    UNKNOWN = "unknown"
    FREE = "free"
    PAID = "paid"
    MIXED = "mixed"


class IntegrationRole(StrEnum):
    """What an integration is used for.

    There is one role in this phase. It is modelled as a role rather than a
    boolean so that a second role can be added later without a migration,
    but no general-purpose routing framework is built.
    """

    HIGHLIGHT_DETECTION = "highlight_detection"


class ConnectionStatus(StrEnum):
    """The outcome of the last connection test."""

    UNKNOWN = "unknown"
    OK = "ok"
    AUTH_FAILED = "auth_failed"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    UNSUPPORTED = "unsupported"
    INVALID_CONFIGURATION = "invalid_configuration"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """What a model can do, and how that was found out.

    Every field is optional, and ``None`` means *not known* rather than
    *not supported*. A capability is only ever sent to a provider when it
    is known to be supported.
    """

    #: The total context the model accepts, in tokens. This is the whole
    #: window, not the desired length of the answer.
    context_window: int | None = None

    #: The largest answer the model will produce, in tokens.
    max_output_tokens: int | None = None

    #: The name of the reasoning-effort parameter, when the model has one.
    #: ``None`` means the model has no such parameter, or that it is not
    #: known -- in both cases nothing is sent.
    reasoning_effort_parameter: str | None = None

    #: The values the reasoning-effort parameter accepts.
    reasoning_effort_values: tuple[str, ...] = ()

    #: How the model can be asked for structured output.
    structured_output: StructuredOutputMode = StructuredOutputMode.NONE

    #: Whether the model is currently offered by the provider.
    available: bool | None = None

    #: Where this metadata came from: a preset, the provider's model list,
    #: or the user's own override.
    metadata_source: str | None = None

    #: When the metadata was last checked.
    verified_at: str | None = None

    def supports_reasoning_effort(self) -> bool:
        """Whether a reasoning-effort value may be sent at all."""
        return bool(self.reasoning_effort_parameter) and bool(
            self.reasoning_effort_values
        )

    def accepts_reasoning_effort(self, value: str | None) -> bool:
        """Whether this particular value is one the model accepts."""
        if value is None:
            return False
        if not self.supports_reasoning_effort():
            return False
        return value in self.reasoning_effort_values


@dataclass(frozen=True, slots=True)
class IntegrationRecord:
    """A saved provider configuration, as it is stored."""

    id: str
    name: str
    provider_id: str
    protocol: ApiProtocol
    base_url: str
    model_id: str | None = None
    model_name: str | None = None
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    pricing_status: PricingStatus = PricingStatus.UNKNOWN
    local_endpoint: bool = False
    enabled: bool = True
    #: A reference into the operating system's credential store. This is
    #: **not** the key, and it is never returned to the browser.
    secret_ref: str | None = None
    role: IntegrationRole = IntegrationRole.HIGHLIGHT_DETECTION
    is_default: bool = False
    last_tested_at: str | None = None
    last_test_status: ConnectionStatus = ConnectionStatus.UNKNOWN
    last_test_code: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def api_key_configured(self) -> bool:
        """Whether a key has been stored for this configuration."""
        return self.secret_ref is not None

    @property
    def protocol_is_implemented(self) -> bool:
        """Whether ClipForge can speak this protocol at all."""
        return self.protocol in IMPLEMENTED_PROTOCOLS

    def is_usable(self) -> bool:
        """Whether this configuration could be used for an analysis."""
        return (
            self.enabled
            and self.protocol_is_implemented
            and bool(self.base_url)
            and bool(self.model_id)
        )

    def snapshot(self) -> dict[str, object]:
        """The values that must not change under a queued analysis.

        A queued analysis records these, and refuses to run if they have
        moved, so that an analysis always runs against the configuration the
        user actually asked for.
        """
        return {
            "provider_id": self.provider_id,
            "protocol": self.protocol.value,
            "base_url": self.base_url,
            "model_id": self.model_id,
        }


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    """One model as the provider lists it.

    The identifier is the exact string the provider expects and is never
    rewritten. The rest is metadata, and anything the provider did not say
    stays unknown.
    """

    id: str
    name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    structured_output: StructuredOutputMode = StructuredOutputMode.NONE
    reasoning_effort_values: tuple[str, ...] = ()
    available: bool | None = None
    #: The protocol this model speaks, when the provider or the preset says.
    protocol: ApiProtocol | None = None
    #: The endpoint the model was discovered at.
    source_url: str | None = None

    def to_capabilities(self, *, verified_at: str | None = None) -> ModelCapabilities:
        """Turn the discovered metadata into capabilities."""
        return ModelCapabilities(
            context_window=self.context_window,
            max_output_tokens=self.max_output_tokens,
            reasoning_effort_parameter=(
                "reasoning_effort" if self.reasoning_effort_values else None
            ),
            reasoning_effort_values=self.reasoning_effort_values,
            structured_output=self.structured_output,
            available=self.available,
            metadata_source="provider_model_list",
            verified_at=verified_at,
        )
