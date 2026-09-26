from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx2
import pytest
from fastapi.testclient import TestClient

from research_agent.site import Page, SiteMap
from research_agent.web import SessionStore, create_app

from conftest import message

SITE = SiteMap([
    Page("/shipments", "Shipments"),
    Page("/shipments/{id}", "Shipment details"),
    Page("/fleet", "Fleet"),
])
SEARCH_CALL = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "diesel price"}}
SEARCH_RESULT = {
    "type": "web_search_tool_result",
    "tool_use_id": "srvtoolu_1",
    "content": [{"type": "web_search_result", "url": "https://eia.example/diesel", "title": "EIA", "encrypted_content": "x", "page_age": None}],
}
CITATION = {"type": "web_search_result_location", "url": "https://eia.example/diesel", "title": "EIA", "cited_text": "c", "encrypted_index": "e"}


def text(s: str, citations: list | None = None) -> dict:
    return {"type": "text", "text": s, "citations": citations}


def navigate_call(path: str, go_now: bool) -> dict:
    return {"type": "tool_use", "id": "toolu_nav", "name": "navigate_to", "input": {"path": path, "go_now": go_now}}


def events(response) -> list[tuple[str, dict]]:
    out = []
    for chunk in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def make_client(api):
    def make(**kwargs) -> TestClient:
        return TestClient(create_app(SITE, site_name="Logistix", client=api.client(), **kwargs))
    return make


def chat(client: TestClient, message_text: str, session_id: str | None = None):
    return client.post("/api/research-agent/chat", json={"session_id": session_id, "message": message_text})


def test_streams_progress_and_answer(api, make_client):
    api.queue(message([text("Checking."), SEARCH_CALL, SEARCH_RESULT, text("Diesel averages $3.80", [CITATION])], searches=1))
    client = make_client()

    res = chat(client, "What's the diesel price?")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    evs = events(res)
    assert evs[0][0] == "session" and evs[0][1]["session_id"]
    assert ("status", {"kind": "search", "text": "Searching: diesel price"}) in evs
    assert "".join(d["delta"] for e, d in evs if e == "text") == "Checking.Diesel averages $3.80"
    kind, done = evs[-1]
    assert kind == "done"
    assert done == {
        "answer": "Diesel averages $3.80",
        "sources": [{"url": "https://eia.example/diesel", "title": "EIA"}],
        "navigate": None,
        "truncated": False,
    }

    req = api.requests[0]
    assert "built into Logistix" in req["system"]
    assert "- /shipments/{id} — Shipment details" in req["system"]
    assert [t["name"] for t in req["tools"]] == ["web_search", "web_fetch", "navigate_to"]
    assert req["output_config"] == {"effort": "medium"}


def test_navigates_to_a_page(api, make_client):
    api.queue(
        message([navigate_call("/shipments/SH-1042", True)], stop_reason="tool_use"),
        message([text("Opening shipment SH-1042.")]),
    )
    client = make_client()

    done = events(chat(client, "Take me to shipment SH-1042"))[-1][1]

    assert done["navigate"] == {"path": "/shipments/SH-1042", "title": "Shipment details", "go_now": True}
    assert done["answer"] == "Opening shipment SH-1042."
    tool_result = api.requests[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "toolu_nav" and not tool_result.get("is_error")


def test_rejects_paths_not_on_the_site_map(api, make_client):
    api.queue(
        message([navigate_call("https://evil.example", True)], stop_reason="tool_use"),
        message([text("That page isn't available.")]),
    )
    client = make_client()

    evs = events(chat(client, "Go to evil"))

    assert evs[-1][1]["navigate"] is None
    assert api.requests[1]["messages"][-1]["content"][0]["is_error"] is True
    assert any(e == "status" and d["kind"] == "error" for e, d in evs)


def test_follow_ups_share_a_session(api, make_client):
    api.queue(message([text("One.")]), message([text("Two.")]), message([text("Fresh.")]))
    client = make_client()

    session_id = events(chat(client, "First"))[0][1]["session_id"]
    evs = events(chat(client, "Follow-up", session_id))
    assert evs[0][1]["session_id"] == session_id
    assert [m["role"] for m in api.requests[1]["messages"]] == ["user", "assistant", "user"]

    # Unknown or expired IDs start a new conversation.
    evs = events(chat(client, "Hello", "not-a-real-session"))
    assert evs[0][1]["session_id"] != session_id
    assert len(api.requests[2]["messages"]) == 1


def test_navigation_does_not_leak_into_the_next_reply(api, make_client):
    api.queue(
        message([navigate_call("/fleet", False)], stop_reason="tool_use"),
        message([text("Fleet has your trucks.")]),
        message([text("Unrelated answer.")]),
    )
    client = make_client()

    first = events(chat(client, "Where are my trucks?"))
    session_id = first[0][1]["session_id"]
    second = events(chat(client, "Thanks", session_id))

    assert first[-1][1]["navigate"]["go_now"] is False
    assert second[-1][1]["navigate"] is None


def test_reset_forgets_the_conversation(api, make_client):
    api.queue(message([text("One.")]), message([text("Two.")]))
    client = make_client()
    session_id = events(chat(client, "First"))[0][1]["session_id"]

    assert client.post("/api/research-agent/reset", json={"session_id": session_id}).json() == {"ok": True}

    evs = events(chat(client, "Again", session_id))
    assert evs[0][1]["session_id"] != session_id


def test_requires_authorization(api, make_client):
    client = make_client(authorize=lambda request: request.headers.get("x-user"))

    assert chat(client, "Hi").status_code == 401
    api.queue(message([text("Hello.")]))
    res = client.post("/api/research-agent/chat", json={"message": "Hi"}, headers={"x-user": "alice"})
    assert res.status_code == 200


def test_sessions_belong_to_their_user(api, make_client):
    api.queue(message([text("For Alice.")]), message([text("For Bob.")]))
    client = make_client(authorize=lambda request: request.headers.get("x-user"))

    alice = client.post("/api/research-agent/chat", json={"message": "Hi"}, headers={"x-user": "alice"})
    alice_id = events(alice)[0][1]["session_id"]
    bob = client.post("/api/research-agent/chat", json={"message": "Hi", "session_id": alice_id}, headers={"x-user": "bob"})

    assert events(bob)[0][1]["session_id"] != alice_id
    assert len(api.requests[1]["messages"]) == 1  # Bob never sees Alice's history


def test_one_message_at_a_time_per_session(api, make_client):
    api.queue(message([text("One.")]))
    client = make_client()
    session_id = events(chat(client, "First"))[0][1]["session_id"]
    session = client.app.state.research_sessions._sessions[session_id]

    session.lock.acquire()
    try:
        res = chat(client, "Second", session_id)
    finally:
        session.lock.release()

    assert res.status_code == 409


class ShipmentLookup:
    """Example ERP data tool, scoped to one user's company."""

    def __init__(self, user: str):
        self.user = user

    @property
    def tool_definitions(self):
        return [{
            "name": "lookup_shipment",
            "description": "Look up one of the user's shipments by ID.",
            "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            "eager_input_streaming": True,
        }]

    def run_tool(self, name, tool_input):
        return f"{tool_input['id']} for {self.user}: in transit, ETA Friday", False


def test_extra_tools_per_user(api, make_client):
    api.queue(
        message([{"type": "tool_use", "id": "toolu_s", "name": "lookup_shipment", "input": {"id": "SH-9"}}], stop_reason="tool_use"),
        message([text("SH-9 arrives Friday.")]),
    )
    client = make_client(
        authorize=lambda request: request.headers.get("x-user"),
        tools_for_user=lambda user: [ShipmentLookup(user)],
    )

    res = client.post("/api/research-agent/chat", json={"message": "Where is SH-9?"}, headers={"x-user": "acme"})

    assert events(res)[-1][1]["answer"] == "SH-9 arrives Friday."
    assert [t["name"] for t in api.requests[0]["tools"]] == ["web_search", "web_fetch", "navigate_to", "lookup_shipment"]
    result = api.requests[1]["messages"][-1]["content"][0]
    assert result["content"] == "SH-9 for acme: in transit, ETA Friday"


def test_errors_are_reported_without_internals(api, make_client):
    api.queue(httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "secret detail"}}))
    client = make_client()

    evs = events(chat(client, "Question"))

    assert evs[-1][0] == "error"
    assert "secret detail" not in evs[-1][1]["message"]


def test_validates_messages(make_client):
    client = make_client()
    assert chat(client, "").status_code == 422
    assert chat(client, "x" * 4001).status_code == 422


def test_serves_the_widget(make_client):
    res = make_client().get("/api/research-agent/widget.js")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]
    assert "window.ResearchAgent" in res.text


@dataclass
class FakeSession:
    owner: str
    last_used: float = field(default_factory=time.monotonic)


def test_session_store_evicts_idle_and_excess_sessions():
    store = SessionStore(FakeSession, max_sessions=2, ttl_seconds=60)

    a, _ = store.get_or_create(None, "u")
    b, _ = store.get_or_create(None, "u")
    c, _ = store.get_or_create(None, "u")  # evicts the least recently used (a)
    assert set(store._sessions) == {b, c}

    for session in store._sessions.values():
        session.last_used -= 120
    d, _ = store.get_or_create(None, "u")
    assert set(store._sessions) == {d}
