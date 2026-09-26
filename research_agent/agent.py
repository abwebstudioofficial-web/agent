"""The research agent: a streaming agentic loop over Claude's web tools.

Web search and web fetch run on Anthropic's servers, so Claude searches and reads
pages inside a single API call. This loop only has to:

- stream each response to a Display so the user can watch progress,
- resume turns the server paused (stop_reason "pause_turn"),
- run client-side tools (your own code, e.g. reading saved reports) and send results back,
- keep the conversation history valid for follow-up questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

import anthropic

from .prompts import build_system_prompt
from .reports import Source, collect_sources

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
MAX_TOKENS = 64000
MAX_STEPS = 40  # API calls per question; stops a runaway loop
MAX_JSON_RETRIES = 2

# On a safety-classifier decline, the API re-runs the request on the model
# Anthropic recommends for that refusal category, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SERVER_TOOLS: list[dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]


class AgentError(Exception):
    """The agent could not finish the turn; the question was not answered."""


class Refused(AgentError):
    """Every model in the fallback chain declined the request."""

    def __init__(self, category: str | None, explanation: str | None):
        self.category = category
        self.explanation = explanation
        detail = f" ({category})" if category else ""
        super().__init__(f"The request was declined{detail}." + (f" {explanation}" if explanation else ""))


class Display(Protocol):
    """Receives progress while the agent works. The CLI renders it to the terminal."""

    def text(self, delta: str) -> None: ...
    def thinking(self, delta: str) -> None: ...
    def thinking_started(self) -> None: ...
    def tool_call(self, name: str, tool_input: dict[str, Any]) -> None: ...
    def tool_error(self, name: str, error: str) -> None: ...
    def fallback(self, from_model: str, to_model: str) -> None: ...
    def notice(self, message: str) -> None: ...


class ToolProvider(Protocol):
    """A group of client-side tools: their definitions plus the code that runs them."""

    @property
    def tool_definitions(self) -> list[dict[str, Any]]: ...

    def run_tool(self, name: str, tool_input: Any) -> tuple[str, bool]:
        """Run one call and return (result text, is_error)."""
        ...


class NullDisplay:
    def text(self, delta: str) -> None: pass
    def thinking(self, delta: str) -> None: pass
    def thinking_started(self) -> None: pass
    def tool_call(self, name: str, tool_input: dict[str, Any]) -> None: pass
    def tool_error(self, name: str, error: str) -> None: pass
    def fallback(self, from_model: str, to_model: str) -> None: pass
    def notice(self, message: str) -> None: pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    web_fetches: int = 0

    def add(self, usage: Any) -> None:
        self.input_tokens += (
            usage.input_tokens
            + (usage.cache_read_input_tokens or 0)
            + (usage.cache_creation_input_tokens or 0)
        )
        self.output_tokens += usage.output_tokens
        if usage.server_tool_use is not None:
            self.web_searches += usage.server_tool_use.web_search_requests
            self.web_fetches += usage.server_tool_use.web_fetch_requests


@dataclass
class Answer:
    question: str
    report: str
    model: str
    sources: list[Source] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    truncated: bool = False


class ResearchAgent:
    def __init__(
        self,
        client: anthropic.Anthropic,
        *,
        tools: Sequence[ToolProvider] = (),
        system: str | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        show_thinking: bool = False,
        display: Display | None = None,
        max_steps: int = MAX_STEPS,
    ):
        self.client = client
        self.model = model
        self.effort = effort
        self.show_thinking = show_thinking
        self.display: Display = display or NullDisplay()
        self.max_steps = max_steps
        self.system = system or build_system_prompt()
        self.tools = list(SERVER_TOOLS)
        self._tool_owners: dict[str, ToolProvider] = {}
        for provider in tools:
            for definition in provider.tool_definitions:
                self.tools.append(definition)
                self._tool_owners[definition["name"]] = provider
        self.messages: list[dict[str, Any]] = []

    def reset(self) -> None:
        """Forget the conversation so the next question starts fresh."""
        self.messages = []

    def ask(self, question: str) -> Answer:
        """Research one question, continuing the current conversation.

        If the turn fails for any reason (API error, refusal, Ctrl-C), the history
        is rolled back so the conversation can carry on as if it was never asked.
        """
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": question})
        try:
            return self._run(question)
        except BaseException:
            del self.messages[checkpoint:]
            raise

    # -- the loop -------------------------------------------------------------

    def _run(self, question: str) -> Answer:
        usage = Usage()
        sources: dict[str, Source] = {}
        json_retries = 0

        for _ in range(self.max_steps):
            try:
                response = self._stream_once()
            except ValueError:
                # The SDK could not parse a client tool's streamed input at all. It
                # raised before the tool_use block completed, so there is no id to
                # answer; re-issue the request. API errors are not ValueErrors.
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    raise AgentError("Claude kept producing unreadable tool input.") from None
                self.display.notice("retrying: tool input was malformed")
                continue
            json_retries = 0
            usage.add(response.usage)

            if response.stop_reason == "refusal":
                details = response.stop_details
                raise Refused(getattr(details, "category", None), getattr(details, "explanation", None))

            content = history_content(response.content)
            for source in collect_sources(content):
                sources.setdefault(source.url, source)
            self.messages.append({"role": "assistant", "content": content})

            if response.stop_reason == "pause_turn":
                # The server-side tool loop hit its iteration limit. Sending the
                # history back as-is resumes it; no extra user message is needed.
                continue

            tool_uses = [b for b in content if b.type == "tool_use"]
            if not tool_uses:
                return Answer(
                    question=question,
                    report=final_text(response.content),
                    model=response.model,
                    sources=list(sources.values()),
                    usage=usage,
                    truncated=response.stop_reason == "max_tokens",
                )
            if response.stop_reason == "max_tokens":
                # A cut-off tool input can still parse as a valid partial object.
                raise AgentError("A tool call was cut off by the output limit.")

            self.messages.append({"role": "user", "content": self._run_tools(tool_uses)})

        raise AgentError(f"Stopped after {self.max_steps} steps without a final answer.")

    def _stream_once(self) -> Any:
        thinking: dict[str, Any] = {"type": "adaptive"}
        if self.show_thinking:
            thinking["display"] = "summarized"

        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=self.system,
            messages=self.messages,
            tools=self.tools,
            thinking=thinking,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            for event in stream:
                self._render(event)
            return stream.get_final_message()

    def _render(self, event: Any) -> None:
        if event.type == "text":
            self.display.text(event.text)
        elif event.type == "thinking":
            self.display.thinking(event.thinking)
        elif event.type == "content_block_start":
            block = event.content_block
            if block.type == "thinking":
                self.display.thinking_started()
            elif block.type == "fallback":
                self.display.fallback(block.from_.model, block.to.model)
        elif event.type == "content_block_stop":
            # The stop event carries the fully accumulated block, input included.
            block = event.content_block
            if block.type in ("server_tool_use", "tool_use"):
                self.display.tool_call(block.name, block.input if isinstance(block.input, dict) else {})
            elif block.type in ("web_search_tool_result", "web_fetch_tool_result"):
                error = getattr(block.content, "error_code", None)
                if error:
                    self.display.tool_error(block.type.removesuffix("_tool_result"), str(error))

    def _run_tools(self, tool_uses: list[Any]) -> list[dict[str, Any]]:
        # All results go back in a single user message.
        results = []
        for block in tool_uses:
            owner = self._tool_owners.get(block.name)
            if owner is None:
                content, is_error = f"Unknown tool: {block.name}", True
            else:
                content, is_error = owner.run_tool(block.name, block.input)
            result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": content}
            if is_error:
                result["is_error"] = True
                self.display.tool_error(block.name, content)
            results.append(result)
        return results


# -- response helpers ---------------------------------------------------------

def history_content(content: list[Any]) -> list[Any]:
    """The blocks of a response to keep in the conversation history.

    After a mid-output fallback, the declined model's partial output stays in the
    response ahead of the last `fallback` block. Only its text and completed
    server-tool calls may be echoed back; thinking, client tool calls, and
    unpaired server-tool calls from before the boundary must be dropped.
    """
    boundary = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    if boundary < 0:
        return list(content)

    before = content[:boundary]
    call_ids = {b.id for b in before if b.type == "server_tool_use"}
    result_ids = {getattr(b, "tool_use_id", None) for b in before if b.type.endswith("_tool_result")}
    paired = call_ids & result_ids

    kept = []
    for block in before:
        if block.type == "text":
            kept.append(block)
        elif block.type == "server_tool_use" and block.id in paired:
            kept.append(block)
        elif block.type.endswith("_tool_result") and getattr(block, "tool_use_id", None) in paired:
            kept.append(block)
    return kept + list(content[boundary + 1 :])


def final_text(content: list[Any]) -> str:
    """The answer: text after the last non-text block (tool call, result, or fallback).

    Earlier text is Claude's running commentary between searches, or partial
    output from a model that declined and handed off.
    """
    tail: list[str] = []
    for block in reversed(content):
        if block.type in ("thinking", "redacted_thinking"):
            continue
        if block.type != "text":
            break
        tail.append(block.text)
    return "".join(reversed(tail)).strip()
