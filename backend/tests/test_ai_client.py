"""The provider protocols, the transport, and the preset catalog.

Every test here runs without a network and without a credential.
"""

from __future__ import annotations

import httpx2
import pytest

from app.ai_client import (
    AiClient,
    AiClientError,
    PROBE_MAX_OUTPUT_TOKENS,
    is_blocked_address,
    parse_model_list,
    validate_base_url,
)
from app.errors import ErrorCode
from app.integrations import (
    ApiProtocol,
    ModelCapabilities,
    PricingStatus,
    StructuredOutputMode,
)
from app.integrations import IntegrationRecord
from app.protocols import (
    AnthropicMessagesAdapter,
    LlmCall,
    OpenAiChatCompletionsAdapter,
    OpenAiResponsesAdapter,
    get_adapter,
)
from app.providers import PROVIDER_PRESETS, get_preset
from fake_provider import (
    FakeProvider,
    anthropic_answer,
    chat_completion,
    responses_answer,
)


def _integration(
    *,
    protocol: ApiProtocol = ApiProtocol.OPENAI_CHAT_COMPLETIONS,
    base_url: str = "https://api.example.test/v1",
    capabilities: ModelCapabilities | None = None,
    local_endpoint: bool = False,
) -> IntegrationRecord:
    """An integration to make requests with."""
    return IntegrationRecord(
        id="i" * 32,
        name="Test",
        provider_id="custom",
        protocol=protocol,
        base_url=base_url,
        model_id="test-model",
        capabilities=capabilities or ModelCapabilities(),
        local_endpoint=local_endpoint,
    )


def _client(provider: FakeProvider, **kwargs) -> AiClient:
    """A client that talks to the fake provider and never sleeps.

    The resolver is replaced so that the destination check runs without a
    DNS lookup: the test host is treated as a public address.
    """
    kwargs.setdefault("max_retries", 0)
    return AiClient(
        transport=provider.transport(),
        sleep=lambda _seconds: None,
        resolver=lambda _host: ["8.8.8.8"],
        **kwargs,
    )


# --- the protocol adapters --------------------------------------------------


def test_the_chat_completions_request_has_the_expected_shape() -> None:
    call = LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="be helpful",
        user="hello",
        api_key="secret",
    )

    prepared = OpenAiChatCompletionsAdapter().prepare(call)

    assert prepared.url == "https://api.example.test/v1/chat/completions"
    assert prepared.headers["Authorization"] == "Bearer secret"
    assert prepared.body["model"] == "test-model"
    assert prepared.body["messages"] == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hello"},
    ]


def test_the_responses_request_has_the_expected_shape() -> None:
    call = LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="be helpful",
        user="hello",
        api_key="secret",
    )

    prepared = OpenAiResponsesAdapter().prepare(call)

    assert prepared.url == "https://api.example.test/v1/responses"
    assert prepared.body["instructions"] == "be helpful"
    assert prepared.body["input"] == "hello"
    # The Responses API does not take a message list.
    assert "messages" not in prepared.body


def test_the_anthropic_request_uses_its_own_authentication_and_path() -> None:
    call = LlmCall(
        base_url="https://api.example.test/anthropic/v1",
        model="claude-test",
        system="be helpful",
        user="hello",
        api_key="secret",
    )

    prepared = AnthropicMessagesAdapter().prepare(call)

    assert prepared.url == "https://api.example.test/anthropic/v1/messages"
    assert prepared.headers["x-api-key"] == "secret"
    assert prepared.headers["anthropic-version"] == "2023-06-01"
    # Anthropic does not use a bearer token.
    assert "Authorization" not in prepared.headers
    # The system prompt is its own field, not a message.
    assert prepared.body["system"] == "be helpful"
    assert prepared.body["messages"] == [{"role": "user", "content": "hello"}]
    # Anthropic requires a maximum token count.
    assert prepared.body["max_tokens"] > 0


def test_a_missing_key_means_no_authorization_header() -> None:
    call = LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="s",
        user="u",
    )

    prepared = OpenAiChatCompletionsAdapter().prepare(call)

    assert "Authorization" not in prepared.headers


def test_the_reasoning_effort_is_omitted_when_the_model_does_not_support_it() -> None:
    call = LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="s",
        user="u",
        reasoning_effort="high",
    )

    prepared = OpenAiChatCompletionsAdapter().prepare(call)

    assert "reasoning_effort" not in prepared.body


def test_the_reasoning_effort_is_sent_when_the_model_supports_it() -> None:
    capabilities = ModelCapabilities(
        reasoning_effort_parameter="reasoning_effort",
        reasoning_effort_values=("low", "medium", "high"),
    )
    call = LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="s",
        user="u",
        capabilities=capabilities,
        reasoning_effort="high",
        max_output_tokens=512,
    )

    prepared = OpenAiChatCompletionsAdapter().prepare(call)

    assert prepared.body["reasoning_effort"] == "high"
    # OpenAI's reasoning models want the newer name for the output budget.
    assert prepared.body["max_completion_tokens"] == 512
    assert "max_tokens" not in prepared.body


def test_an_unknown_reasoning_effort_value_is_not_sent() -> None:
    capabilities = ModelCapabilities(
        reasoning_effort_parameter="reasoning_effort",
        reasoning_effort_values=("low", "high"),
    )
    call = LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="s",
        user="u",
        capabilities=capabilities,
        reasoning_effort="extreme",
    )

    prepared = OpenAiChatCompletionsAdapter().prepare(call)

    assert "reasoning_effort" not in prepared.body


def test_anthropic_never_sends_a_reasoning_effort() -> None:
    capabilities = ModelCapabilities(
        reasoning_effort_parameter="reasoning_effort",
        reasoning_effort_values=("low", "high"),
    )
    call = LlmCall(
        base_url="https://api.example.test/anthropic/v1",
        model="claude-test",
        system="s",
        user="u",
        capabilities=capabilities,
        reasoning_effort="high",
    )

    prepared = AnthropicMessagesAdapter().prepare(call)

    assert "reasoning_effort" not in prepared.body
    assert "reasoning" not in prepared.body


def test_structured_output_is_requested_when_the_model_supports_it() -> None:
    capabilities = ModelCapabilities(
        structured_output=StructuredOutputMode.JSON_SCHEMA
    )
    call = LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="s",
        user="u",
        capabilities=capabilities,
        json_schema={"type": "object"},
        json_schema_name="clipforge_highlights",
    )

    prepared = OpenAiChatCompletionsAdapter().prepare(call)

    assert prepared.body["response_format"]["type"] == "json_schema"
    assert (
        prepared.body["response_format"]["json_schema"]["name"]
        == "clipforge_highlights"
    )


def test_structured_output_is_not_requested_when_the_model_cannot_do_it() -> None:
    call = LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="s",
        user="u",
        json_schema={"type": "object"},
    )

    prepared = OpenAiChatCompletionsAdapter().prepare(call)

    assert "response_format" not in prepared.body


def test_each_protocol_reads_its_own_answer() -> None:
    assert (
        OpenAiChatCompletionsAdapter().read_text(chat_completion("hello")) == "hello"
    )
    assert OpenAiResponsesAdapter().read_text(responses_answer("hello")) == "hello"
    assert AnthropicMessagesAdapter().read_text(anthropic_answer("hello")) == "hello"


def test_a_malformed_answer_reads_as_empty() -> None:
    assert OpenAiChatCompletionsAdapter().read_text({"nonsense": True}) == ""
    assert OpenAiResponsesAdapter().read_text("not a dict") == ""
    assert AnthropicMessagesAdapter().read_text({"content": "not a list"}) == ""


def test_an_unimplemented_protocol_has_no_adapter() -> None:
    assert get_adapter(ApiProtocol.GOOGLE_GENERATE_CONTENT) is None
    assert get_adapter(ApiProtocol.SYSTEM_ONE) is None


# --- URL validation and SSRF ------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test/v1",
        "https://127.0.0.1/v1",
        "https://localhost/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/v1",
        "https://192.168.1.1/v1",
        "https://[::1]/v1",
    ],
)
def test_a_remote_url_may_not_reach_a_prohibited_destination(url: str) -> None:
    with pytest.raises(AiClientError) as failure:
        validate_base_url(url)

    assert failure.value.code == ErrorCode.AI_UNSAFE_URL.value


def test_a_remote_url_must_use_https() -> None:
    with pytest.raises(AiClientError) as failure:
        validate_base_url("http://api.example.test/v1")

    assert failure.value.code == ErrorCode.AI_UNSAFE_URL.value
    assert "https" in failure.value.message


def test_a_local_endpoint_is_allowed_when_it_is_asked_for_explicitly() -> None:
    assert (
        validate_base_url("http://127.0.0.1:11434/v1", allow_local=True)
        == "http://127.0.0.1:11434/v1"
    )


def test_a_url_may_not_contain_credentials() -> None:
    with pytest.raises(AiClientError) as failure:
        validate_base_url("https://user:pass@api.example.test/v1")

    assert failure.value.code == ErrorCode.AI_UNSAFE_URL.value


def test_a_url_may_not_use_another_scheme() -> None:
    with pytest.raises(AiClientError):
        validate_base_url("file:///etc/passwd")


def test_the_url_is_normalised() -> None:
    assert (
        validate_base_url("https://api.example.test/v1/")
        == "https://api.example.test/v1"
    )
    assert (
        validate_base_url("api.example.test/v1")
        == "https://api.example.test/v1"
    )


def test_the_blocked_address_check_covers_the_usual_ranges() -> None:
    assert is_blocked_address("127.0.0.1")
    assert is_blocked_address("10.1.2.3")
    assert is_blocked_address("169.254.169.254")
    assert is_blocked_address("::1")
    assert not is_blocked_address("8.8.8.8")


# --- the transport ----------------------------------------------------------


def test_a_successful_completion_returns_the_text() -> None:
    provider = FakeProvider()
    provider.push(chat_completion("ready"))

    text = _client(provider).complete(_integration(), "secret", LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="s",
        user="u",
        api_key="secret",
    ))

    assert text == "ready"
    assert provider.last_request.url == "https://api.example.test/v1/chat/completions"


def test_the_key_is_sent_as_a_bearer_token() -> None:
    provider = FakeProvider()
    provider.push(chat_completion("ready"))

    _client(provider).complete(_integration(), "the-key", LlmCall(
        base_url="https://api.example.test/v1",
        model="test-model",
        system="s",
        user="u",
        api_key="the-key",
    ))

    assert provider.last_request.headers["authorization"] == "Bearer the-key"


def test_an_authentication_failure_is_classified() -> None:
    provider = FakeProvider()
    provider.push({"error": {"message": "invalid api key"}}, status=401)

    with pytest.raises(AiClientError) as failure:
        _client(provider).complete(_integration(), "wrong", LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_AUTH_FAILED.value
    # The provider's own body never reaches the user.
    assert "invalid api key" not in failure.value.message


def test_a_rate_limit_is_classified() -> None:
    provider = FakeProvider()
    provider.push({"error": "slow down"}, status=429)

    with pytest.raises(AiClientError) as failure:
        _client(provider, max_retries=0).complete(_integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_RATE_LIMITED.value


def test_a_retry_after_is_honoured() -> None:
    provider = FakeProvider()
    provider.push({"error": "slow down"}, status=429, headers={"retry-after": "7"})
    provider.push({"error": "slow down"}, status=429)
    slept: list[float] = []
    client = AiClient(
        transport=provider.transport(),
        max_retries=1,
        sleep=slept.append,
        resolver=lambda _host: ["8.8.8.8"],
    )

    with pytest.raises(AiClientError) as failure:
        client.complete(_integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_RATE_LIMITED.value
    # The provider said how long to wait, and that is what was waited.
    assert slept == [7.0]


def test_a_rate_limit_is_retried_and_can_succeed() -> None:
    provider = FakeProvider()
    provider.push({"error": "slow down"}, status=429)
    provider.push(chat_completion("ready"))

    text = _client(provider, max_retries=1).complete(
        _integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        )
    )

    assert text == "ready"
    assert len(provider.requests) == 2


def test_an_authentication_failure_is_never_retried() -> None:
    provider = FakeProvider()
    provider.push({"error": "no"}, status=401)
    provider.push(chat_completion("ready"))

    with pytest.raises(AiClientError):
        _client(provider, max_retries=3).complete(
            _integration(), None, LlmCall(
                base_url="https://api.example.test/v1",
                model="test-model",
                system="s",
                user="u",
            )
        )

    # One attempt only: trying again cannot help.
    assert len(provider.requests) == 1


def test_a_provider_failure_is_retried() -> None:
    provider = FakeProvider()
    provider.push({"error": "boom"}, status=503)
    provider.push(chat_completion("ready"))

    text = _client(provider, max_retries=1).complete(
        _integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        )
    )

    assert text == "ready"


def test_a_timeout_is_classified() -> None:
    provider = FakeProvider()
    provider.push_error(httpx2.TimeoutException("too slow"))

    with pytest.raises(AiClientError) as failure:
        _client(provider).complete(_integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_TIMEOUT.value


def test_a_transport_failure_is_classified() -> None:
    provider = FakeProvider()
    provider.push_error(httpx2.ConnectError("no route"))

    with pytest.raises(AiClientError) as failure:
        _client(provider).complete(_integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_UNAVAILABLE.value


def test_a_redirect_is_refused_rather_than_followed() -> None:
    provider = FakeProvider()
    provider.push({"moved": True}, status=302, headers={"location": "https://evil.test"})

    with pytest.raises(AiClientError) as failure:
        _client(provider).complete(_integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_PROTOCOL_MISMATCH.value
    # One request only: the redirect was not chased.
    assert len(provider.requests) == 1


def test_a_missing_model_is_distinguished_from_a_missing_endpoint() -> None:
    provider = FakeProvider()
    provider.push({"error": {"message": "model 'test-model' not found"}}, status=404)

    with pytest.raises(AiClientError) as failure:
        _client(provider).complete(_integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_MODEL_UNAVAILABLE.value


def test_an_unknown_endpoint_is_reported_as_a_mismatch() -> None:
    provider = FakeProvider()
    provider.push({"error": {"message": "not found"}}, status=404)

    with pytest.raises(AiClientError) as failure:
        _client(provider).complete(_integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_PROTOCOL_MISMATCH.value


def test_a_response_larger_than_the_cap_is_refused() -> None:
    provider = FakeProvider()
    provider.push({"padding": "x" * 5000})

    with pytest.raises(AiClientError) as failure:
        _client(provider, max_response_bytes=1000).complete(
            _integration(), None, LlmCall(
                base_url="https://api.example.test/v1",
                model="test-model",
                system="s",
                user="u",
            )
        )

    assert failure.value.code == ErrorCode.AI_RESPONSE_TOO_LARGE.value


def test_an_empty_answer_is_a_protocol_error() -> None:
    provider = FakeProvider()
    provider.push(chat_completion("   "))

    with pytest.raises(AiClientError) as failure:
        _client(provider).complete(_integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_PROTOCOL_ERROR.value


def test_an_unimplemented_protocol_is_refused_before_any_request() -> None:
    provider = FakeProvider()

    with pytest.raises(AiClientError) as failure:
        _client(provider).complete(
            _integration(protocol=ApiProtocol.GOOGLE_GENERATE_CONTENT),
            None,
            LlmCall(
                base_url="https://api.example.test/v1",
                model="m",
                system="s",
                user="u",
            ),
        )

    assert failure.value.code == ErrorCode.AI_NOT_SUPPORTED.value
    assert provider.requests == []


# --- model discovery --------------------------------------------------------


def test_the_model_list_is_read() -> None:
    provider = FakeProvider()
    provider.push(
        {
            "object": "list",
            "data": [
                {"id": "model-a", "max_context_length": 131072},
                {"id": "model-b", "context_window": 8192},
            ],
        }
    )

    models = _client(provider).list_models(
        _integration(), None, models_url="https://api.example.test/v1/models"
    )

    assert [model.id for model in models] == ["model-a", "model-b"]
    assert models[0].context_window == 131072
    assert models[1].context_window == 8192
    assert provider.last_request.url == "https://api.example.test/v1/models"


def test_a_model_list_in_another_shape_is_still_read() -> None:
    provider = FakeProvider()
    provider.push({"models": [{"id": "model-a"}]})

    models = _client(provider).list_models(
        _integration(), None, models_url="https://api.example.test/v1/models"
    )

    assert [model.id for model in models] == ["model-a"]


def test_a_malformed_model_list_is_a_protocol_error() -> None:
    provider = FakeProvider()
    provider.push({"unexpected": True})

    with pytest.raises(AiClientError) as failure:
        _client(provider).list_models(
            _integration(), None, models_url="https://api.example.test/v1/models"
        )

    assert failure.value.code == ErrorCode.AI_PROTOCOL_ERROR.value


def test_a_model_list_error_is_classified() -> None:
    provider = FakeProvider()
    provider.push({"error": "no"}, status=401)

    with pytest.raises(AiClientError) as failure:
        _client(provider).list_models(
            _integration(), None, models_url="https://api.example.test/v1/models"
        )

    assert failure.value.code == ErrorCode.AI_AUTH_FAILED.value


def test_the_model_list_parser_never_invents_metadata() -> None:
    models = parse_model_list({"data": [{"id": "model-a"}]})

    assert models[0].context_window is None
    assert models[0].max_output_tokens is None
    assert models[0].available is None


# --- the connection test ----------------------------------------------------


def test_a_successful_connection_test_says_so() -> None:
    provider = FakeProvider()
    provider.push(chat_completion("ready"))

    outcome = _client(provider).test_connection(_integration(), "secret")

    assert outcome.status.value == "ok"
    assert outcome.reply == "ready"
    # The test sends no user content.
    assert "clip" not in str(provider.last_request.json).lower()


def test_the_connection_probe_leaves_room_for_reasoning_tokens() -> None:
    """A reasoning model spends invisible tokens before the visible answer.

    A probe budget that is too tight comes back empty and fails a perfectly
    good provider -- the reasoning-model case that shipped this fix.
    """
    provider = FakeProvider()
    provider.push(chat_completion("ready"))

    outcome = _client(provider).test_connection(_integration(), "secret")

    assert outcome.status.value == "ok"
    assert provider.last_request.json["max_tokens"] == PROBE_MAX_OUTPUT_TOKENS


def test_an_empty_reply_at_the_output_limit_says_what_to_do() -> None:
    provider = FakeProvider()
    provider.push(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning": "the model thought and thought",
                    },
                }
            ]
        }
    )

    outcome = _client(provider).test_connection(_integration(), "secret")

    assert outcome.status.value == "provider_error"
    assert outcome.code == ErrorCode.AI_PROTOCOL_ERROR.value
    assert "reasoning" in outcome.message
    assert "output limit" in outcome.message


def test_a_connection_test_without_a_model_is_refused() -> None:
    record = IntegrationRecord(
        id="i" * 32,
        name="Test",
        provider_id="custom",
        protocol=ApiProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url="https://api.example.test/v1",
    )
    provider = FakeProvider()

    outcome = _client(provider).test_connection(record, None)

    assert outcome.status.value == "invalid_configuration"
    assert provider.requests == []


def test_a_connection_test_with_an_unsupported_protocol_says_so() -> None:
    record = _integration(protocol=ApiProtocol.GOOGLE_GENERATE_CONTENT)
    provider = FakeProvider()

    outcome = _client(provider).test_connection(record, None)

    assert outcome.status.value == "unsupported"
    assert provider.requests == []


def test_a_connection_test_reports_an_authentication_failure() -> None:
    provider = FakeProvider()
    provider.push({"error": "no"}, status=401)

    outcome = _client(provider).test_connection(_integration(), "wrong")

    assert outcome.status.value == "auth_failed"
    assert outcome.code == ErrorCode.AI_AUTH_FAILED.value


def test_a_host_that_resolves_to_a_prohibited_address_is_refused() -> None:
    """The destination is checked again, immediately before the request."""
    provider = FakeProvider()
    client = AiClient(
        transport=provider.transport(),
        max_retries=0,
        sleep=lambda _seconds: None,
        resolver=lambda _host: ["169.254.169.254"],
    )

    with pytest.raises(AiClientError) as failure:
        client.complete(_integration(), None, LlmCall(
            base_url="https://api.example.test/v1",
            model="test-model",
            system="s",
            user="u",
        ))

    assert failure.value.code == ErrorCode.AI_UNSAFE_URL.value
    # The request was never sent.
    assert provider.requests == []


def test_a_local_endpoint_skips_the_destination_check() -> None:
    """A local model server is allowed to be on this machine."""
    provider = FakeProvider()
    provider.push(chat_completion("ready"))
    client = AiClient(
        transport=provider.transport(),
        max_retries=0,
        sleep=lambda _seconds: None,
        resolver=lambda _host: ["127.0.0.1"],
    )

    text = client.complete(
        _integration(
            base_url="http://127.0.0.1:11434/v1", local_endpoint=True
        ),
        None,
        LlmCall(
            base_url="http://127.0.0.1:11434/v1",
            model="test-model",
            system="s",
            user="u",
        ),
    )

    assert text == "ready"


# --- the preset catalog -----------------------------------------------------


def test_the_catalog_holds_the_documented_providers() -> None:
    ids = {preset.id for preset in PROVIDER_PRESETS}

    assert {"opencode_zen", "groq", "cerebras", "custom"} <= ids


def test_the_presets_hold_their_documented_base_urls() -> None:
    groq = get_preset("groq")
    cerebras = get_preset("cerebras")
    zen = get_preset("opencode_zen")

    assert groq is not None and cerebras is not None and zen is not None
    assert (
        groq.base_url_for(ApiProtocol.OPENAI_CHAT_COMPLETIONS)
        == "https://api.groq.com/openai/v1"
    )
    assert (
        cerebras.base_url_for(ApiProtocol.OPENAI_CHAT_COMPLETIONS)
        == "https://api.cerebras.ai/v1"
    )
    assert zen.base_url_for(ApiProtocol.ANTHROPIC_MESSAGES) is not None


def test_the_opencode_preset_routes_model_families_to_their_protocol() -> None:
    zen = get_preset("opencode_zen")
    assert zen is not None

    assert zen.protocol_for_model("claude-sonnet-5") is ApiProtocol.ANTHROPIC_MESSAGES
    assert zen.protocol_for_model("gpt-5.5") is ApiProtocol.OPENAI_RESPONSES
    assert (
        zen.protocol_for_model("deepseek-v4-pro")
        is ApiProtocol.OPENAI_CHAT_COMPLETIONS
    )
    # A model the hint does not cover is *unknown*, not guessed.
    assert zen.protocol_for_model("some-future-model") is None


def test_no_preset_claims_a_model_is_free_forever() -> None:
    """The catalog records a tier, and says that it can change."""
    for preset in PROVIDER_PRESETS:
        assert preset.free_tier in ("trial", "unknown", "none")
        note = preset.free_tier_note.lower()
        # The note always points at the provider as the authority, and
        # always says the answer can change.
        assert "provider" in note or "check" in note
