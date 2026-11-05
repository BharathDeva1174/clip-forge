"""A stand-in for an AI provider.

The tests use this instead of a real provider: it records the requests
that were made and answers with canned responses, so every failure mode
can be provoked without a network and without a credential.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2


@dataclass
class RecordedRequest:
    """One request the fake provider received."""

    method: str
    url: str
    headers: dict[str, str]
    body: Any

    @property
    def json(self) -> dict[str, Any]:
        """The request body, parsed."""
        return self.body if isinstance(self.body, dict) else {}


@dataclass
class CannedResponse:
    """One answer the fake provider will give."""

    status: int = 200
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    #: When set, the transport raises this instead of answering.
    raises: Exception | None = None


class FakeProvider:
    """A provider that answers whatever the test tells it to."""

    def __init__(self, responses: list[CannedResponse] | None = None) -> None:
        self.requests: list[RecordedRequest] = []
        self._responses = list(responses or [])

    def push(
        self,
        payload: Any = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Queue an answer."""
        self._responses.append(
            CannedResponse(status=status, payload=payload, headers=headers or {})
        )

    def push_error(self, error: Exception) -> None:
        """Queue a transport failure."""
        self._responses.append(CannedResponse(raises=error))

    def transport(self) -> httpx2.MockTransport:
        """The transport to give to the client."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            body: Any = None
            raw = request.content
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = raw.decode("utf-8", "replace")
            self.requests.append(
                RecordedRequest(
                    method=request.method,
                    url=str(request.url),
                    headers={k.lower(): v for k, v in request.headers.items()},
                    body=body,
                )
            )

            if not self._responses:
                return httpx2.Response(
                    500, json={"error": {"message": "no answer was queued"}}
                )
            canned = self._responses.pop(0)
            if canned.raises is not None:
                raise canned.raises
            if isinstance(canned.payload, (dict, list)):
                return httpx2.Response(
                    canned.status, json=canned.payload, headers=canned.headers
                )
            return httpx2.Response(
                canned.status,
                text="" if canned.payload is None else str(canned.payload),
                headers=canned.headers,
            )

        return httpx2.MockTransport(handler)

    @property
    def last_request(self) -> RecordedRequest:
        """The most recent request."""
        return self.requests[-1]


def chat_completion(text: str) -> dict[str, Any]:
    """A Chat Completions answer carrying this text."""
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def responses_answer(text: str) -> dict[str, Any]:
    """A Responses API answer carrying this text."""
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


def anthropic_answer(text: str) -> dict[str, Any]:
    """An Anthropic Messages answer carrying this text."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-test",
        "stop_reason": "end_turn",
    }
