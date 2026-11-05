"""The HTTP transport for AI providers.

This module is the only place that talks to the network on ClipForge's
behalf. Everything it does is bounded and checked:

- **The URL is validated before anything is sent.** A remote integration
  must use HTTPS. A local endpoint is allowed, but only when the
  configuration says so explicitly -- the check is not weakened globally
  for everyone's sake.
- **The destination is checked.** The host is resolved and every address it
  resolves to is examined. Loopback, private, link-local, unspecified,
  multicast and reserved addresses are refused for a remote integration, so
  a configuration cannot be used to reach a service on the user's own
  network or a cloud metadata endpoint.
- **Redirects are not followed.** A redirect to another origin would carry
  the authorization header with it, so a 3xx is an error rather than
  something to chase.
- **Every request is bounded.** A timeout, a response-size cap and a
  bounded retry budget with backoff. ``Retry-After`` is honoured.
- **The provider's own error body never reaches the user.** It is
  inspected, so the failure can be classified, and then summarised.

Secrets are never logged, never put in a URL and never included in an
error message.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import random
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx2

from app.errors import ErrorCode
from app.integrations import (
    ApiProtocol,
    ConnectionStatus,
    DiscoveredModel,
    IntegrationRecord,
    ModelCapabilities,
    StructuredOutputMode,
)
from app.protocols import LlmCall, PreparedCall, get_adapter

logger = logging.getLogger("clipforge.ai")

#: The default request timeout, in seconds.
DEFAULT_TIMEOUT_SECONDS = 60.0

#: The default number of retries after the first attempt.
DEFAULT_MAX_RETRIES = 2

#: The largest response body that is read, in bytes.
DEFAULT_MAX_RESPONSE_BYTES = 1_000_000

#: The longest a retry backoff may grow, in seconds.
_MAX_BACKOFF_SECONDS = 30.0

#: Hostnames that name a cloud metadata service. They are refused by name,
#: because they are how a metadata service is usually reached.
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)


class AiClientError(Exception):
    """A provider request that did not succeed.

    The message is safe to show a user: it never contains a key, a raw
    response body or a transcript.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after

    @property
    def status(self) -> ConnectionStatus:
        """The connection status this failure maps to."""
        return _CONNECTION_STATUS_BY_CODE.get(self.code, ConnectionStatus.PROVIDER_ERROR)


_CONNECTION_STATUS_BY_CODE: dict[str, ConnectionStatus] = {
    ErrorCode.AI_AUTH_FAILED.value: ConnectionStatus.AUTH_FAILED,
    ErrorCode.AI_MODEL_UNAVAILABLE.value: ConnectionStatus.MODEL_UNAVAILABLE,
    ErrorCode.AI_PROTOCOL_MISMATCH.value: ConnectionStatus.PROTOCOL_MISMATCH,
    ErrorCode.AI_RATE_LIMITED.value: ConnectionStatus.RATE_LIMITED,
    ErrorCode.AI_TIMEOUT.value: ConnectionStatus.TIMEOUT,
    ErrorCode.AI_NOT_SUPPORTED.value: ConnectionStatus.UNSUPPORTED,
    ErrorCode.AI_INVALID_CONFIGURATION.value: ConnectionStatus.INVALID_CONFIGURATION,
    ErrorCode.AI_UNSAFE_URL.value: ConnectionStatus.INVALID_CONFIGURATION,
    ErrorCode.AI_INVALID_REQUEST.value: ConnectionStatus.PROVIDER_ERROR,
    ErrorCode.AI_RESPONSE_TOO_LARGE.value: ConnectionStatus.PROVIDER_ERROR,
    ErrorCode.AI_PROTOCOL_ERROR.value: ConnectionStatus.PROVIDER_ERROR,
    ErrorCode.AI_UNAVAILABLE.value: ConnectionStatus.PROVIDER_ERROR,
}

#: The output budget of the one-word connection probe. A reasoning model
#: spends invisible reasoning tokens before it writes the visible answer, so
#: a probe that is too tight comes back with an empty reply and a
#: ``finish_reason`` of ``length`` -- which looks exactly like a broken
#: provider. The budget is generous because the probe is tiny and rare.
PROBE_MAX_OUTPUT_TOKENS = 1024


@dataclass(frozen=True, slots=True)
class ConnectionOutcome:
    """What a connection test found."""

    status: ConnectionStatus
    code: str
    message: str
    #: The model the provider actually answered with, when it said.
    model: str | None = None
    #: The text the provider answered with, for a successful test.
    reply: str | None = None


# --- URL validation ---------------------------------------------------------


def is_blocked_address(address: str) -> bool:
    """Whether an address must not be reached by a remote integration.

    Loopback, private, link-local (which includes the cloud metadata
    address), unspecified, multicast and reserved addresses are all
    refused, because a remote integration has no business talking to them.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    return bool(
        parsed.is_loopback
        or parsed.is_private
        or parsed.is_link_local
        or parsed.is_unspecified
        or parsed.is_multicast
        or parsed.is_reserved
    )


def validate_base_url(url: str, *, allow_local: bool = False) -> str:
    """Check a base URL *syntactically* and return it normalised.

    This is the check that runs when a configuration is saved, so it must
    not need a network: it looks at the URL, not at what the host resolves
    to. The destination itself is checked again, immediately before every
    request, by :meth:`AiClient._check_destination`.

    Raises:
        AiClientError: with ``ai_unsafe_url`` when the URL may not be used.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise AiClientError(
            ErrorCode.AI_INVALID_CONFIGURATION.value,
            "The base URL is empty.",
        )
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise AiClientError(
            ErrorCode.AI_UNSAFE_URL.value,
            "The base URL must use http or https.",
        )
    if parsed.username or parsed.password:
        raise AiClientError(
            ErrorCode.AI_UNSAFE_URL.value,
            "The base URL must not contain a username or a password.",
        )
    if parsed.fragment:
        raise AiClientError(
            ErrorCode.AI_UNSAFE_URL.value,
            "The base URL must not contain a fragment.",
        )
    host = (parsed.hostname or "").strip()
    if not host:
        raise AiClientError(
            ErrorCode.AI_INVALID_CONFIGURATION.value,
            "The base URL has no host.",
        )

    if scheme == "http" and not allow_local:
        raise AiClientError(
            ErrorCode.AI_UNSAFE_URL.value,
            "A remote provider must use https. Turn on the local endpoint "
            "option to use a plain http endpoint on your own machine.",
        )

    if not allow_local:
        # A host that is already an address is checked without a lookup.
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and is_blocked_address(str(address)):
            raise AiClientError(
                ErrorCode.AI_UNSAFE_URL.value,
                f"The address {host} is not allowed for a remote provider. Use "
                "the local endpoint option if you mean a service on this "
                "machine.",
            )
        if host == "localhost" or host.endswith(".localhost"):
            raise AiClientError(
                ErrorCode.AI_UNSAFE_URL.value,
                "A remote provider may not be on this machine. Use the local "
                "endpoint option if you mean a service running here.",
            )
        if host in _METADATA_HOSTNAMES:
            raise AiClientError(
                ErrorCode.AI_UNSAFE_URL.value,
                "That host is a cloud metadata service and is not allowed.",
            )

    path = parsed.path.rstrip("/")
    return f"{scheme}://{parsed.netloc}{path}"


# --- the client -------------------------------------------------------------


class AiClient:
    """Makes provider requests, within bounds.

    The transport can be replaced, which is how the tests exercise every
    failure mode without a network and without a real credential.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx2.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        resolver: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_response_bytes = max_response_bytes
        self._transport = transport
        self._sleep = sleep
        self._resolver = resolver or _resolve_host

    def _check_destination(self, url: str, *, allow_local: bool) -> None:
        """Check where a request is about to go.

        The host is resolved and every address it resolves to is examined.
        This runs immediately before the request, so the answer is as fresh
        as it can be -- a name that points at the user's own network, or at
        a cloud metadata address, is refused.

        The residual risk of a name that resolves to a permitted address
        once and a forbidden one later is documented in the Phase 4 report.
        """
        host = urlparse(url).hostname or ""
        if not host or allow_local:
            return
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            # Already checked when the URL was validated.
            return

        try:
            addresses = self._resolver(host)
        except OSError as exc:
            raise AiClientError(
                ErrorCode.AI_UNAVAILABLE.value,
                "The provider's host could not be resolved.",
            ) from exc

        for resolved in addresses:
            if is_blocked_address(resolved):
                raise AiClientError(
                    ErrorCode.AI_UNSAFE_URL.value,
                    f"The host {host!r} resolves to {resolved}, which is not "
                    "allowed for a remote provider. Use the local endpoint "
                    "option if you mean a service on this machine.",
                )

    def _client(self) -> httpx2.Client:
        """A client that never follows a redirect and never uses a proxy.

        ``trust_env`` is off so that a proxy setting in the environment
        cannot silently redirect a request, and the timeout is explicit.
        """
        return httpx2.Client(
            transport=self._transport,
            timeout=httpx2.Timeout(self.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )

    # -- model discovery -----------------------------------------------------

    def list_models(
        self,
        integration: IntegrationRecord,
        api_key: str | None,
        *,
        models_url: str,
    ) -> list[DiscoveredModel]:
        """Ask the provider which models it offers.

        Raises:
            AiClientError: with a stable code for every failure mode.
        """
        url = validate_base_url(
            models_url, allow_local=integration.local_endpoint
        )
        self._check_destination(url, allow_local=integration.local_endpoint)
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        status, body = self._request("GET", url, headers=headers, body=None)
        if status >= 400:
            raise self._error_for_status(status, body, model=integration.model_id)

        models = parse_model_list(body, source_url=url)
        if not models:
            raise AiClientError(
                ErrorCode.AI_PROTOCOL_ERROR.value,
                "The provider's model list could not be read.",
            )
        return models

    # -- completion ----------------------------------------------------------

    def complete(
        self,
        integration: IntegrationRecord,
        api_key: str | None,
        call: LlmCall,
    ) -> str:
        """Ask the model for an answer.

        Raises:
            AiClientError: with a stable code for every failure mode.
        """
        adapter = get_adapter(integration.protocol)
        if adapter is None:
            raise AiClientError(
                ErrorCode.AI_NOT_SUPPORTED.value,
                "ClipForge cannot speak this API protocol.",
            )

        base_url = validate_base_url(
            call.base_url or integration.base_url,
            allow_local=integration.local_endpoint,
        )
        self._check_destination(base_url, allow_local=integration.local_endpoint)
        prepared = adapter.prepare(
            LlmCall(
                base_url=base_url,
                model=call.model,
                system=call.system,
                user=call.user,
                api_key=api_key,
                capabilities=call.capabilities,
                reasoning_effort=call.reasoning_effort,
                max_output_tokens=call.max_output_tokens,
                temperature=call.temperature,
                json_schema=call.json_schema,
                json_schema_name=call.json_schema_name,
            )
        )

        status, body = self._request(
            "POST", prepared.url, headers=prepared.headers, body=prepared.body
        )
        if status >= 400:
            raise self._error_for_status(status, body, model=call.model)

        text = adapter.read_text(body)
        if not text.strip():
            raise AiClientError(
                ErrorCode.AI_PROTOCOL_ERROR.value,
                "The provider answered, but the reply was empty."
                + _empty_reply_hint(body),
            )
        return text

    # -- connection test -----------------------------------------------------

    def test_connection(
        self,
        integration: IntegrationRecord,
        api_key: str | None,
    ) -> ConnectionOutcome:
        """Try the configuration, and say what happened.

        The test asks the provider to say one word. It never sends a
        transcript, a video or any user content.
        """
        adapter = get_adapter(integration.protocol)
        if adapter is None:
            return ConnectionOutcome(
                status=ConnectionStatus.UNSUPPORTED,
                code=ErrorCode.AI_NOT_SUPPORTED.value,
                message="ClipForge cannot speak this API protocol.",
            )
        if not integration.model_id:
            return ConnectionOutcome(
                status=ConnectionStatus.INVALID_CONFIGURATION,
                code=ErrorCode.AI_INVALID_CONFIGURATION.value,
                message="Choose a model before testing the connection.",
            )

        try:
            reply = self.complete(
                integration,
                api_key,
                LlmCall(
                    base_url=integration.base_url,
                    model=integration.model_id,
                    system="You are a connectivity probe. Answer with one word.",
                    user="Reply with the single word: ready",
                    api_key=api_key,
                    capabilities=integration.capabilities,
                    max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
                    temperature=0.0,
                ),
            )
        except AiClientError as exc:
            return ConnectionOutcome(
                status=exc.status, code=exc.code, message=exc.message
            )

        return ConnectionOutcome(
            status=ConnectionStatus.OK,
            code="connection_ok",
            message="The provider answered.",
            model=integration.model_id,
            reply=reply.strip()[:200],
        )

    # -- the request itself --------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[int, object]:
        """Send a request, with a bounded retry budget.

        Returns the status code and the parsed body. A body that is not
        JSON is returned as a string, so the failure can still be
        classified.
        """
        attempts = self.max_retries + 1
        last_error: AiClientError | None = None

        for attempt in range(attempts):
            try:
                status, payload, retry_after = self._send_once(
                    method, url, headers, body
                )
            except AiClientError as exc:
                last_error = exc
                if not self._is_retryable(exc) or attempt == attempts - 1:
                    raise
                self._sleep(self._backoff(attempt, None))
                continue

            if status == 429 and attempt < attempts - 1:
                self._sleep(self._backoff(attempt, retry_after))
                continue
            if status >= 500 and attempt < attempts - 1:
                self._sleep(self._backoff(attempt, retry_after))
                continue
            return status, payload

        raise last_error or AiClientError(  # pragma: no cover - defensive
            ErrorCode.AI_UNAVAILABLE.value,
            "The provider could not be reached.",
        )

    def _send_once(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[int, object, float | None]:
        """One attempt."""
        try:
            with self._client() as client:
                with client.stream(
                    method, url, headers=headers, json=body
                ) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        # A redirect would take the authorization header to
                        # another origin, so it is refused rather than
                        # followed.
                        raise AiClientError(
                            ErrorCode.AI_PROTOCOL_MISMATCH.value,
                            "The endpoint redirected the request. ClipForge "
                            "does not follow redirects, because that could "
                            "send the key to another host.",
                        )
                    retry_after = _retry_after_seconds(response.headers)
                    raw = self._read_capped(response)
        except AiClientError:
            raise
        except httpx2.TimeoutException as exc:
            raise AiClientError(
                ErrorCode.AI_TIMEOUT.value,
                "The provider did not answer in time.",
            ) from exc
        except httpx2.TransportError as exc:
            raise AiClientError(
                ErrorCode.AI_UNAVAILABLE.value,
                "The provider could not be reached.",
            ) from exc

        return status, _parse_body(raw), retry_after

    def _read_capped(self, response: httpx2.Response) -> bytes:
        """Read the body, refusing to read more than the cap."""
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > self.max_response_bytes:
                raise AiClientError(
                    ErrorCode.AI_RESPONSE_TOO_LARGE.value,
                    "The provider's answer was larger than the limit.",
                )
        return bytes(body)

    def _is_retryable(self, error: AiClientError) -> bool:
        """Whether another attempt could plausibly succeed.

        Authentication failures and invalid configuration are never retried:
        trying again cannot help, and it would waste the user's quota.
        """
        return error.code in (
            ErrorCode.AI_TIMEOUT.value,
            ErrorCode.AI_UNAVAILABLE.value,
        )

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """How long to wait before the next attempt, with jitter.

        Jitter prevents retry storms when several windows or analyses back off
        at the same time.
        """
        if retry_after is not None:
            return max(0.0, min(retry_after, _MAX_BACKOFF_SECONDS))
        base = min(2.0**attempt, _MAX_BACKOFF_SECONDS)
        # Add up to 25% jitter so concurrent retries do not land together.
        return base + random.uniform(0.0, base * 0.25)

    def _retry_after(self, payload: object) -> float | None:
        """Deprecated in favour of the header value; kept for clarity."""
        return None

    def _error_for_status(
        self, status: int, payload: object, *, model: str | None
    ) -> AiClientError:
        """Classify a failure into a stable code.

        The provider's own body is inspected but never shown: it may carry
        account details, and it is not the user's error message.
        """
        detail = _body_text(payload).lower()

        if status in (401, 403):
            return AiClientError(
                ErrorCode.AI_AUTH_FAILED.value,
                "The provider rejected the credentials. Check the API key.",
            )
        if status == 404:
            if model and _mentions_missing_model(detail, model):
                return AiClientError(
                    ErrorCode.AI_MODEL_UNAVAILABLE.value,
                    "The provider does not offer that model.",
                )
            return AiClientError(
                ErrorCode.AI_PROTOCOL_MISMATCH.value,
                "The endpoint was not found. Check the base URL and that the "
                "protocol matches the provider.",
            )
        if status == 429:
            return AiClientError(
                ErrorCode.AI_RATE_LIMITED.value,
                "The provider is rate limiting this account. Try again in a "
                "moment.",
            )
        # Context-limit rejections are detectable in the provider's own body,
        # but not every 400 means the context was exceeded: inspect the body
        # before classifying.
        if status in (400, 404, 405, 409, 413, 415, 422):
            if _is_context_limit(detail):
                return AiClientError(
                    ErrorCode.AI_CONTEXT_LIMIT_EXCEEDED.value,
                    "The provider rejected the request because the prompt "
                    "exceeded its context limit. The analysis will retry with "
                    "a smaller transcript window.",
                )
            return AiClientError(
                ErrorCode.AI_INVALID_REQUEST.value,
                "The provider rejected the request. Check the model, the "
                "protocol and the settings.",
            )
        if status >= 500:
            return AiClientError(
                ErrorCode.AI_UNAVAILABLE.value,
                "The provider reported a temporary failure.",
            )
        return AiClientError(
            ErrorCode.AI_UNAVAILABLE.value,
            "The provider could not be reached.",
        )


def _is_context_limit(detail: str) -> bool:
    """Whether a provider's error body describes a context-limit rejection."""
    return any(
        phrase in detail
        for phrase in (
            "context",
            "maximum context",
            "context length",
            "context window",
            "token limit",
            "maximum tokens",
            "too many tokens",
            "exceeds the maximum",
        )
    )


def _resolve_host(host: str) -> list[str]:
    """The addresses a host resolves to."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def _retry_after_seconds(headers: Any) -> float | None:
    """The ``Retry-After`` the provider asked for, if it sent a usable one."""
    raw = None
    try:
        raw = headers.get("retry-after")
    except AttributeError:  # pragma: no cover - defensive
        return None
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _mentions_missing_model(detail: str, model: str) -> bool:
    """Whether a provider's error says the *model* was not found."""
    if model.lower() in detail:
        return True
    return "model" in detail and any(
        phrase in detail for phrase in ("not found", "does not exist", "unknown", "no such")
    )


def _empty_reply_hint(body: object) -> str:
    """A hint about an empty reply, when the body says why it was empty."""
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish = choices[0].get("finish_reason")
            if finish == "length":
                return (
                    " The model stopped at its output limit before writing an"
                    " answer -- it most likely spent the whole budget on"
                    " reasoning. Give the integration a larger output budget"
                    " or retry the test."
                )
    return ""


def _body_text(payload: object) -> str:
    """The text of a response body, for classification only."""
    if isinstance(payload, str):
        return payload[:4000]
    if isinstance(payload, dict):
        try:
            return json.dumps(payload)[:4000]
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return ""
    return ""


def _parse_body(raw: bytes) -> object:
    """Parse a response body as JSON, or keep it as text."""
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


# --- model list parsing -----------------------------------------------------


def parse_model_list(payload: object, *, source_url: str | None = None) -> list[DiscoveredModel]:
    """Read a provider's model list, tolerantly but never inventing.

    The common shape is OpenAI's ``{"data": [{"id": ...}]}``. Some providers
    use ``{"models": [...]}`` or a bare list. Whatever the shape, the model
    identifier is taken exactly as the provider wrote it, and any metadata
    the provider did not supply stays unknown.
    """
    entries = _model_entries(payload)
    models: list[DiscoveredModel] = []
    for entry in entries:
        if isinstance(entry, str):
            models.append(DiscoveredModel(id=entry, source_url=source_url))
            continue
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id") or entry.get("name")
        if not isinstance(identifier, str) or not identifier.strip():
            continue
        models.append(
            DiscoveredModel(
                id=identifier.strip(),
                name=_display_name(entry),
                context_window=_first_int(
                    entry,
                    "context_window",
                    "max_context_length",
                    "context_length",
                    "max_model_len",
                    "max_input_tokens",
                ),
                max_output_tokens=_first_int(
                    entry,
                    "max_output_tokens",
                    "max_completion_tokens",
                    "max_tokens",
                ),
                structured_output=_structured_output(entry),
                available=_available(entry),
                source_url=source_url,
            )
        )
    return models


def _model_entries(payload: object) -> list[object]:
    """The list of model entries inside a provider's response."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models", "model_list"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _display_name(entry: dict[str, Any]) -> str | None:
    """A human-readable name, when the provider gave one."""
    for key in ("display_name", "name", "title", "label"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_int(entry: dict[str, Any], *keys: str) -> int | None:
    """The first of ``keys`` that holds a usable integer."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            number = int(value.strip())
            if number > 0:
                return number
    return None


def _structured_output(entry: dict[str, Any]) -> StructuredOutputMode:
    """Whether the provider advertised structured output."""
    features = entry.get("features")
    if isinstance(features, list):
        names = {str(item).lower() for item in features}
        if "structured_outputs" in names or "structured_output" in names:
            return StructuredOutputMode.JSON_SCHEMA
        if "json_object" in names:
            return StructuredOutputMode.JSON_OBJECT
    if entry.get("supports_structured_outputs") is True:
        return StructuredOutputMode.JSON_SCHEMA
    return StructuredOutputMode.NONE


def _available(entry: dict[str, Any]) -> bool | None:
    """Whether the provider said the model is available."""
    value = entry.get("available")
    if isinstance(value, bool):
        return value
    status = entry.get("status")
    if isinstance(status, str):
        lowered = status.strip().lower()
        if lowered in ("available", "active", "ready"):
            return True
        if lowered in ("deprecated", "retired", "unavailable"):
            return False
    return None
