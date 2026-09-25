"""A fake Messages API: the real SDK client talks to a mock HTTP transport.

Tests script responses as plain message dicts; FakeAPI turns each into the SSE
event stream the API would send, and records every request body it receives.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

_Anthropic = anthropic.Anthropic  # tests may monkeypatch anthropic.Anthropic
import httpx2
import pytest


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def to_sse(message: dict[str, Any]) -> str:
    start = {**message, "content": [], "stop_reason": None, "stop_sequence": None}
    start.pop("stop_details", None)
    out = [_sse({"type": "message_start", "message": start})]

    for i, block in enumerate(message["content"]):
        kind = block["type"]
        if kind == "text":
            out.append(_sse({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}))
            out.append(_sse({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}}))
            for citation in block.get("citations") or []:
                out.append(_sse({"type": "content_block_delta", "index": i, "delta": {"type": "citations_delta", "citation": citation}}))
        elif kind in ("tool_use", "server_tool_use"):
            out.append(_sse({"type": "content_block_start", "index": i, "content_block": {**block, "input": {}}}))
            out.append(_sse({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}))
        elif kind == "thinking":
            out.append(_sse({"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}))
            out.append(_sse({"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": block["thinking"]}}))
            out.append(_sse({"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": block["signature"]}}))
        else:
            out.append(_sse({"type": "content_block_start", "index": i, "content_block": block}))
        out.append(_sse({"type": "content_block_stop", "index": i}))

    delta: dict[str, Any] = {"stop_reason": message["stop_reason"], "stop_sequence": None}
    if message.get("stop_details"):
        delta["stop_details"] = message["stop_details"]
    out.append(_sse({"type": "message_delta", "delta": delta, "usage": message["usage"]}))
    out.append(_sse({"type": "message_stop"}))
    return "".join(out)


def message(
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
    *,
    model: str = "claude-opus-5",
    stop_details: dict[str, Any] | None = None,
    searches: int = 0,
    fetches: int = 0,
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 0,
            "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": fetches},
        },
    }


class FakeAPI:
    def __init__(self) -> None:
        self.responses: list[dict[str, Any] | str | httpx2.Response] = []
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx2.Headers] = []

    def queue(self, *responses: dict[str, Any] | str | httpx2.Response) -> None:
        """Queue message dicts, raw SSE text, or a ready-made HTTP response (for errors)."""
        self.responses.extend(responses)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        if not self.responses:
            raise AssertionError("unexpected extra API request")
        scripted = self.responses.pop(0)
        if isinstance(scripted, httpx2.Response):
            return scripted
        body = scripted if isinstance(scripted, str) else to_sse(scripted)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    def client(self) -> anthropic.Anthropic:
        return _Anthropic(
            api_key="test-key",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handler)),
        )


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()
