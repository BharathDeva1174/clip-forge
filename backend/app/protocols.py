"""The protocol adapters.

Each supported API gets its own adapter. They are **not** assumed to be
alike, because they are not:

- the endpoint path differs;
- the authentication differs -- a bearer token, or an API key header plus a
  version header;
- the request body differs, including where the system prompt goes and what
  the answer is called;
- the reasoning control differs, and one of the three has no equivalent at
  all;
- the response shape differs, and the text is not always in the same place.

An adapter turns a :class:`LlmCall` into a :class:`PreparedCall` and reads
the answer back out of a parsed JSON body. It knows nothing about
transcripts, highlights or timestamps, which is what keeps the highlight
business logic free of provider details.

Optional parameters are only ever sent when the model is known to accept
them. A request that would otherwise work is never broken by sending a
parameter the model does not understand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.integrations import ApiProtocol, ModelCapabilities, StructuredOutputMode

#: The Anthropic Messages API requires a maximum token count on every
#: request, so one is supplied when the configuration does not set one.
DEFAULT_ANTHROPIC_MAX_TOKENS = 4096

#: The version of the Anthropic API this adapter speaks.
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True, slots=True)
class LlmCall:
    """One request to a model, in protocol-neutral terms."""

    base_url: str
    model: str
    system: str
    user: str
    api_key: str | None = None
    capabilities: ModelCapabilities = ModelCapabilities()
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    #: The JSON schema the answer should follow, when the model supports
    #: structured output.
    json_schema: dict[str, Any] | None = None
    #: The name used for the schema in a structured-output request.
    json_schema_name: str = "clipforge_highlights"


@dataclass(frozen=True, slots=True)
class PreparedCall:
    """A request that is ready to be sent."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]


class ProtocolAdapter(Protocol):
    """Turns a call into a request, and a response into text."""

    protocol: ApiProtocol

    def prepare(self, call: LlmCall) -> PreparedCall:
        """Build the request for this protocol."""
        ...

    def read_text(self, payload: object) -> str:
        """Read the model's answer out of a parsed response body."""
        ...


def _join(base_url: str, path: str) -> str:
    """Join a base URL and an endpoint path."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _bearer(api_key: str | None) -> dict[str, str]:
    """The authorization header, when there is a key."""
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _content_to_text(content: object) -> str:
    """Read text out of a content value that may be a string or a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


class OpenAiChatCompletionsAdapter:
    """The OpenAI-compatible Chat Completions API."""

    protocol = ApiProtocol.OPENAI_CHAT_COMPLETIONS

    def prepare(self, call: LlmCall) -> PreparedCall:
        body: dict[str, Any] = {
            "model": call.model,
            "messages": [
                {"role": "system", "content": call.system},
                {"role": "user", "content": call.user},
            ],
        }
        if call.temperature is not None:
            body["temperature"] = call.temperature

        reasoning = self._reasoning(call)
        if reasoning is not None:
            body["reasoning_effort"] = reasoning
            # OpenAI's reasoning models take the output budget under a
            # different name, and reject the older one.
            if call.max_output_tokens is not None:
                body["max_completion_tokens"] = call.max_output_tokens
        elif call.max_output_tokens is not None:
            body["max_tokens"] = call.max_output_tokens

        response_format = self._response_format(call)
        if response_format is not None:
            body["response_format"] = response_format

        return PreparedCall(
            url=_join(call.base_url, "chat/completions"),
            headers={"Content-Type": "application/json", **_bearer(call.api_key)},
            body=body,
        )

    def _reasoning(self, call: LlmCall) -> str | None:
        """The reasoning effort, when the model is known to accept one."""
        if not call.capabilities.accepts_reasoning_effort(call.reasoning_effort):
            return None
        return call.reasoning_effort

    def _response_format(self, call: LlmCall) -> dict[str, Any] | None:
        """The structured-output instruction, when the model supports one."""
        mode = call.capabilities.structured_output
        if mode is StructuredOutputMode.JSON_OBJECT:
            return {"type": "json_object"}
        if mode is StructuredOutputMode.JSON_SCHEMA and call.json_schema is not None:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": call.json_schema_name,
                    "strict": True,
                    "schema": call.json_schema,
                },
            }
        return None

    def read_text(self, payload: object) -> str:
        if not isinstance(payload, dict):
            return ""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        return _content_to_text(message.get("content"))


class OpenAiResponsesAdapter:
    """The OpenAI Responses API."""

    protocol = ApiProtocol.OPENAI_RESPONSES

    def prepare(self, call: LlmCall) -> PreparedCall:
        body: dict[str, Any] = {
            "model": call.model,
            "instructions": call.system,
            "input": call.user,
        }
        if call.temperature is not None:
            body["temperature"] = call.temperature
        if call.max_output_tokens is not None:
            body["max_output_tokens"] = call.max_output_tokens

        # The Responses API spells reasoning as an object, not a flat field.
        if call.capabilities.accepts_reasoning_effort(call.reasoning_effort):
            body["reasoning"] = {"effort": call.reasoning_effort}

        if (
            call.capabilities.structured_output is StructuredOutputMode.JSON_SCHEMA
            and call.json_schema is not None
        ):
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": call.json_schema_name,
                    "strict": True,
                    "schema": call.json_schema,
                }
            }

        return PreparedCall(
            url=_join(call.base_url, "responses"),
            headers={"Content-Type": "application/json", **_bearer(call.api_key)},
            body=body,
        )

    def read_text(self, payload: object) -> str:
        if not isinstance(payload, dict):
            return ""
        output = payload.get("output")
        if not isinstance(output, list):
            return ""
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in (None, "message"):
                continue
            parts.append(_content_to_text(item.get("content")))
        return "".join(parts)


class AnthropicMessagesAdapter:
    """The Anthropic Messages API."""

    protocol = ApiProtocol.ANTHROPIC_MESSAGES

    def prepare(self, call: LlmCall) -> PreparedCall:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if call.api_key:
            headers["x-api-key"] = call.api_key

        body: dict[str, Any] = {
            "model": call.model,
            "system": call.system,
            "messages": [{"role": "user", "content": call.user}],
            # Anthropic requires this on every request.
            "max_tokens": call.max_output_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS,
        }
        if call.temperature is not None:
            body["temperature"] = call.temperature

        # There is deliberately no reasoning parameter here. Anthropic's
        # equivalent is a thinking token budget, which is a different
        # quantity, and sending one under a reasoning-effort name would
        # mean something else. It is omitted rather than mistranslated.

        return PreparedCall(
            url=_join(call.base_url, "messages"),
            headers=headers,
            body=body,
        )

    def read_text(self, payload: object) -> str:
        if not isinstance(payload, dict):
            return ""
        content = payload.get("content")
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in (None, "text"):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)


_ADAPTERS: dict[ApiProtocol, ProtocolAdapter] = {
    ApiProtocol.OPENAI_CHAT_COMPLETIONS: OpenAiChatCompletionsAdapter(),
    ApiProtocol.OPENAI_RESPONSES: OpenAiResponsesAdapter(),
    ApiProtocol.ANTHROPIC_MESSAGES: AnthropicMessagesAdapter(),
}


def get_adapter(protocol: ApiProtocol) -> ProtocolAdapter | None:
    """The adapter for a protocol, when ClipForge implements it."""
    return _ADAPTERS.get(protocol)


def supported_protocols() -> tuple[ApiProtocol, ...]:
    """Every protocol this build can speak."""
    return tuple(_ADAPTERS)
