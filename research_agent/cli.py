"""Command-line interface: ask one question, or chat with follow-ups."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, TextIO

import anthropic

from .agent import DEFAULT_EFFORT, AgentError, Answer, ResearchAgent
from .reports import ReportLibrary

EFFORTS = ["low", "medium", "high", "xhigh", "max"]

HELP = """\
Ask a research question, then follow-ups; the agent remembers the conversation.
Commands:  /new  start a fresh conversation   /help  show this   /exit  quit (or Ctrl-D)"""


class TerminalDisplay:
    """Streams the agent's progress to a terminal, dimming everything but the answer."""

    def __init__(self, out: TextIO | None = None):
        self.out = out or sys.stdout
        color = self.out.isatty()
        self._dim = "\033[2m" if color else ""
        self._reset = "\033[0m" if color else ""
        self._at_line_start = True
        self._in_thinking = False

    def _write(self, s: str) -> None:
        if s:
            self.out.write(s)
            self.out.flush()
            self._at_line_start = s.endswith("\n")

    def _line(self, s: str) -> None:
        """Write a dim status line on its own line."""
        self._end_thinking()
        if not self._at_line_start:
            self._write("\n")
        self._write(f"{self._dim}{s}{self._reset}\n")

    def _end_thinking(self) -> None:
        if self._in_thinking:
            self._in_thinking = False
            self._write(f"{self._reset}\n")

    def text(self, delta: str) -> None:
        self._end_thinking()
        self._write(delta)

    def thinking_started(self) -> None:
        self._line("  · thinking")

    def thinking(self, delta: str) -> None:
        if not delta:
            return
        if not self._in_thinking:
            self._in_thinking = True
            self._write(self._dim)
        self._write(delta)

    def tool_call(self, name: str, tool_input: dict[str, Any]) -> None:
        if name == "web_search":
            self._line(f"  → searching: {tool_input.get('query', '')}")
        elif name == "web_fetch":
            self._line(f"  → reading:   {tool_input.get('url', '')}")
        elif name == "list_reports":
            self._line("  → listing saved reports")
        elif name == "read_report":
            self._line(f"  → reading report: {tool_input.get('filename', '')}")
        else:
            self._line(f"  → {name}")

    def tool_error(self, name: str, error: str) -> None:
        self._line(f"  ! {name} failed: {error}")

    def fallback(self, from_model: str, to_model: str) -> None:
        self._line(f"  ! {from_model} declined; continuing on {to_model}")

    def notice(self, message: str) -> None:
        self._line(f"  ! {message}")

    def finish(self) -> None:
        self._end_thinking()
        if not self._at_line_start:
            self._write("\n")

    def summary(self, s: str) -> None:
        self._write(f"\n{self._dim}{s}{self._reset}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-agent",
        description="A research agent that searches the web and writes cited reports, powered by Claude.",
    )
    parser.add_argument("question", nargs="*", help="Question to research. Omit it to start an interactive session.")
    parser.add_argument(
        "--effort",
        choices=EFFORTS,
        default=DEFAULT_EFFORT,
        help=f"How hard Claude works on each question (default: {DEFAULT_EFFORT}). Lower is faster and cheaper.",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports"),
        help="Where reports are saved and read back from (default: ./reports).",
    )
    parser.add_argument("--no-save", action="store_true", help="Don't save reports to disk.")
    parser.add_argument("--show-thinking", action="store_true", help="Print summaries of Claude's reasoning.")
    return parser


def answer_one(agent: ResearchAgent, display: TerminalDisplay, question: str, save: bool) -> bool:
    """Run one question and print the result. Returns False if it failed."""
    try:
        answer = agent.ask(question)
    except KeyboardInterrupt:
        display.finish()
        display.summary("Cancelled.")
        return False
    except AgentError as e:
        display.finish()
        print(f"Error: {e}", file=sys.stderr)
        return False
    except anthropic.AuthenticationError:
        display.finish()
        print("Error: authentication failed. Check your ANTHROPIC_API_KEY.", file=sys.stderr)
        return False
    except anthropic.RateLimitError:
        display.finish()
        print("Error: rate limited by the API. Wait a moment and try again.", file=sys.stderr)
        return False
    except anthropic.APIConnectionError:
        display.finish()
        print("Error: could not reach the Anthropic API. Check your network connection.", file=sys.stderr)
        return False
    except anthropic.APIStatusError as e:
        display.finish()
        print(f"Error: API returned {e.status_code}: {e.message}", file=sys.stderr)
        return False
    except anthropic.CredentialsError as e:
        display.finish()
        print(f"Error: could not load credentials: {e}", file=sys.stderr)
        return False
    except TypeError as e:
        # The SDK raises TypeError when no credentials are configured at all.
        if "authentication" not in str(e):
            raise
        display.finish()
        print("Error: no API key found. Set ANTHROPIC_API_KEY (see README.md).", file=sys.stderr)
        return False

    display.finish()
    saved_to = None
    if save and answer.report:
        saved_to = agent.library.save(answer.question, answer.report, answer.sources, answer.model)
    display.summary(summary_line(answer, saved_to))
    return True


def summary_line(answer: Answer, saved_to: Path | None) -> str:
    parts = []
    if answer.truncated:
        parts.append("answer was cut off at the output limit")
    if saved_to:
        parts.append(f"saved to {saved_to}")
    u = answer.usage
    parts.append(f"{_plural(u.web_searches, 'search', 'searches')}, {_plural(u.web_fetches, 'page')} read")
    parts.append(f"{u.input_tokens:,} input / {u.output_tokens:,} output tokens")
    return " · ".join(parts)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def repl(agent: ResearchAgent, display: TerminalDisplay, save: bool) -> None:
    print(HELP)
    while True:
        try:
            line = input("\nresearch> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("/exit", "/quit"):
            return
        if line == "/help":
            print(HELP)
            continue
        if line == "/new":
            agent.reset()
            print("Started a new conversation.")
            continue
        print()
        answer_one(agent, display, line, save)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = anthropic.Anthropic()
    except anthropic.CredentialsError as e:
        print(f"Error: could not load credentials: {e}", file=sys.stderr)
        return 1
    display = TerminalDisplay()
    agent = ResearchAgent(
        client,
        ReportLibrary(args.reports_dir),
        effort=args.effort,
        show_thinking=args.show_thinking,
        display=display,
    )
    save = not args.no_save

    if args.question:
        return 0 if answer_one(agent, display, " ".join(args.question), save) else 1
    repl(agent, display, save)
    return 0


if __name__ == "__main__":
    sys.exit(main())
