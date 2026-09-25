from __future__ import annotations

import pytest

from research_agent.agent import FALLBACK_BETA, AgentError, Refused, ResearchAgent
from research_agent.reports import ReportLibrary

from conftest import message

SEARCH_CALL = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "solid state batteries 2026"}}
SEARCH_RESULT = {
    "type": "web_search_tool_result",
    "tool_use_id": "srvtoolu_1",
    "content": [{"type": "web_search_result", "url": "https://a.example/news", "title": "A News", "encrypted_content": "x", "page_age": None}],
}
FETCH_CALL = {"type": "server_tool_use", "id": "srvtoolu_2", "name": "web_fetch", "input": {"url": "https://b.example/paper"}}
FETCH_RESULT = {
    "type": "web_fetch_tool_result",
    "tool_use_id": "srvtoolu_2",
    "content": {
        "type": "web_fetch_result",
        "url": "https://b.example/paper",
        "retrieved_at": None,
        "content": {"type": "document", "title": "B Paper", "source": {"type": "text", "media_type": "text/plain", "data": "..."}},
    },
}
CITATION = {"type": "web_search_result_location", "url": "https://a.example/news", "title": "A News", "cited_text": "c", "encrypted_index": "e"}


def text(s: str, citations: list | None = None) -> dict:
    return {"type": "text", "text": s, "citations": citations}


class RecordingDisplay:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def text(self, delta): self.events.append(("text", delta))
    def thinking(self, delta): self.events.append(("thinking", delta))
    def thinking_started(self): self.events.append(("thinking_started",))
    def tool_call(self, name, tool_input): self.events.append(("tool_call", name, tool_input))
    def tool_error(self, name, error): self.events.append(("tool_error", name, error))
    def fallback(self, from_model, to_model): self.events.append(("fallback", from_model, to_model))
    def notice(self, msg): self.events.append(("notice", msg))


@pytest.fixture
def library(tmp_path) -> ReportLibrary:
    return ReportLibrary(tmp_path / "reports")


def make_agent(api, library, **kwargs) -> tuple[ResearchAgent, RecordingDisplay]:
    display = RecordingDisplay()
    return ResearchAgent(api.client(), library, display=display, **kwargs), display


def test_answers_with_web_tools_and_collects_sources(api, library):
    api.queue(message(
        [
            text("Let me look that up."),
            SEARCH_CALL, SEARCH_RESULT, FETCH_CALL, FETCH_RESULT,
            text("Solid-state batteries are shipping in small volumes", [CITATION]),
            text(" as of 2026."),
        ],
        searches=1, fetches=1,
    ))
    agent, display = make_agent(api, library)

    answer = agent.ask("Where are solid-state batteries at?")

    # Interim commentary before the tools is not part of the report.
    assert answer.report == "Solid-state batteries are shipping in small volumes as of 2026."
    assert [s.url for s in answer.sources] == ["https://b.example/paper", "https://a.example/news"]
    assert answer.sources[0].title == "B Paper"
    assert answer.usage.web_searches == 1 and answer.usage.web_fetches == 1
    assert answer.usage.input_tokens == 150 and answer.usage.output_tokens == 20
    assert ("tool_call", "web_search", {"query": "solid state batteries 2026"}) in display.events
    assert ("tool_call", "web_fetch", {"url": "https://b.example/paper"}) in display.events

    req = api.requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["fallbacks"] == "default"
    assert FALLBACK_BETA in api.headers[0]["anthropic-beta"]
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"] == {"effort": "high"}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["stream"] is True
    tool_names = [t["name"] for t in req["tools"]]
    assert tool_names == ["web_search", "web_fetch", "list_reports", "read_report"]
    assert "Today's date is" in req["system"]

    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


def test_follow_up_questions_send_the_whole_conversation(api, library):
    api.queue(message([text("First answer.")]), message([text("Second answer.")]))
    agent, _ = make_agent(api, library)

    agent.ask("First?")
    agent.ask("And a follow-up?")

    sent = api.requests[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"][0]["text"] == "First answer."

    agent.reset()
    assert agent.messages == []


def test_resumes_paused_turns_without_extra_user_message(api, library):
    api.queue(
        message([SEARCH_CALL, SEARCH_RESULT], stop_reason="pause_turn"),
        message([text("Done.")]),
    )
    agent, _ = make_agent(api, library)

    answer = agent.ask("Question")

    assert answer.report == "Done."
    resumed = api.requests[1]["messages"]
    assert [m["role"] for m in resumed] == ["user", "assistant"]
    assert resumed[1]["content"][0]["type"] == "server_tool_use"


def test_runs_client_tools_and_returns_results(api, library):
    library.save("Old question about lithium", "Old report body.", [], "claude-opus-5")
    api.queue(
        message([
            text("Checking your past reports."),
            {"type": "tool_use", "id": "toolu_1", "name": "list_reports", "input": {}},
        ], stop_reason="tool_use"),
        message([text("You researched lithium before.")]),
    )
    agent, display = make_agent(api, library)

    answer = agent.ask("What have I researched?")

    assert answer.report == "You researched lithium before."
    tool_turn = api.requests[1]["messages"][-1]
    assert tool_turn["role"] == "user"
    result = tool_turn["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
    assert "old-question-about-lithium" in result["content"]
    assert "Old question about lithium" in result["content"]
    assert not result.get("is_error")
    assert ("tool_call", "list_reports", {}) in display.events

    # Client tools stream their input eagerly.
    client_tools = [t for t in api.requests[0]["tools"] if "input_schema" in t]
    assert all(t["eager_input_streaming"] is True for t in client_tools)


def test_refusal_raises_and_rolls_back_history(api, library):
    api.queue(
        message([text("First answer.")]),
        message(
            [text("Partial")],
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "cyber", "explanation": "Not available."},
        ),
    )
    agent, _ = make_agent(api, library)
    agent.ask("Fine question")

    with pytest.raises(Refused) as excinfo:
        agent.ask("Declined question")

    assert excinfo.value.category == "cyber"
    assert "cyber" in str(excinfo.value)
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


def test_mid_output_fallback_keeps_only_valid_blocks(api, library):
    api.queue(message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig-a"},
            text("Partial from the first model."),
            SEARCH_CALL, SEARCH_RESULT,
            {"type": "server_tool_use", "id": "srvtoolu_9", "name": "web_search", "input": {"query": "unpaired"}},
            {"type": "fallback", "from": {"model": "claude-opus-5"}, "to": {"model": "claude-opus-4-8"}, "trigger": {"type": "refusal"}},
            {"type": "thinking", "thinking": "", "signature": "sig-b"},
            text("Answer from the fallback model."),
        ],
        model="claude-opus-4-8",
    ))
    agent, display = make_agent(api, library)

    answer = agent.ask("Question")

    assert answer.report == "Answer from the fallback model."
    assert answer.model == "claude-opus-4-8"
    assert ("fallback", "claude-opus-5", "claude-opus-4-8") in display.events

    kept = agent.messages[1]["content"]
    assert [b.type for b in kept] == ["text", "server_tool_use", "web_search_tool_result", "thinking", "text"]
    assert kept[1].id == "srvtoolu_1"
    assert kept[3].signature == "sig-b"


def test_truncated_tool_call_is_not_run(api, library):
    api.queue(message(
        [{"type": "tool_use", "id": "toolu_1", "name": "read_report", "input": {"filename": "x"}}],
        stop_reason="max_tokens",
    ))
    agent, _ = make_agent(api, library)

    with pytest.raises(AgentError, match="cut off"):
        agent.ask("Question")
    assert agent.messages == []


def test_truncated_answer_is_flagged(api, library):
    api.queue(message([text("A long answer that got cu")], stop_reason="max_tokens"))
    agent, _ = make_agent(api, library)

    answer = agent.ask("Question")

    assert answer.truncated
    assert answer.report == "A long answer that got cu"


def test_malformed_tool_input_is_retried(api, library):
    bad = (
        'event: message_start\ndata: {"type":"message_start","message":{"id":"m","type":"message","role":"assistant",'
        '"model":"claude-opus-5","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"tool_use","id":"toolu_1","name":"read_report","input":{}}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":'
        '{"type":"input_json_delta","partial_json":"{]]"}}\n\n'
    )
    api.queue(bad, message([text("Recovered.")]))
    agent, display = make_agent(api, library)

    answer = agent.ask("Question")

    assert answer.report == "Recovered."
    assert any(e[0] == "notice" for e in display.events)
    assert len(api.requests) == 2


def test_gives_up_after_max_steps(api, library):
    api.queue(*[message([SEARCH_CALL, SEARCH_RESULT], stop_reason="pause_turn") for _ in range(3)])
    agent, _ = make_agent(api, library, max_steps=3)

    with pytest.raises(AgentError, match="3 steps"):
        agent.ask("Question")
    assert agent.messages == []


def test_show_thinking_requests_summaries(api, library):
    api.queue(message([{"type": "thinking", "thinking": "Considering sources.", "signature": "s"}, text("Answer.")]))
    agent, display = make_agent(api, library, show_thinking=True, effort="low")

    agent.ask("Question")

    assert api.requests[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert api.requests[0]["output_config"] == {"effort": "low"}
    assert ("thinking", "Considering sources.") in display.events
