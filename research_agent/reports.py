"""Saving research reports to disk, and the tools that let Claude read them back."""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Source:
    url: str
    title: str | None = None


def collect_sources(content: list[Any]) -> list[Source]:
    """Pages the agent actually read or cited in one response, in order of appearance."""
    found: dict[str, Source] = {}
    for block in content:
        if block.type == "web_fetch_tool_result" and block.content.type == "web_fetch_result":
            doc = block.content.content
            found.setdefault(block.content.url, Source(block.content.url, getattr(doc, "title", None)))
        elif block.type == "text":
            for citation in block.citations or []:
                if citation.type == "web_search_result_location":
                    found.setdefault(citation.url, Source(citation.url, citation.title))
    return list(found.values())


def slugify(text: str, max_words: int = 8) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:max_words]
    return "-".join(words) or "report"


class ReportLibrary:
    """A directory of Markdown reports, one per question answered."""

    def __init__(self, directory: Path):
        self.directory = directory

    # -- saving -------------------------------------------------------------

    def save(self, question: str, report: str, sources: list[Source], model: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now().astimezone()
        stem = f"{now.date().isoformat()}-{slugify(question)}"
        path = self.directory / f"{stem}.md"
        n = 2
        while path.exists():
            path = self.directory / f"{stem}-{n}.md"
            n += 1

        # JSON strings are valid YAML scalars, so the front matter stays parseable
        # whatever characters the question contains.
        lines = [
            "---",
            f"question: {json.dumps(question)}",
            f"date: {now.isoformat(timespec='seconds')}",
            f"model: {model}",
            "---",
            "",
            report.strip(),
            "",
        ]
        if sources:
            lines += ["## Pages consulted", ""]
            lines += [f"- [{s.title or s.url}]({s.url})" for s in sources]
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    # -- tools for Claude -----------------------------------------------------

    @property
    def tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_reports",
                "description": (
                    "List research reports saved from earlier sessions, newest first, with "
                    "each report's file name, date and original question. Call this when the "
                    "user refers to earlier research or asks what they have looked into before."
                ),
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "eager_input_streaming": True,
            },
            {
                "name": "read_report",
                "description": (
                    "Read the full text of one saved report. Call this after list_reports "
                    "when a past report is relevant to the user's question."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "File name exactly as returned by list_reports.",
                        }
                    },
                    "required": ["filename"],
                    "additionalProperties": False,
                },
                "eager_input_streaming": True,
            },
        ]

    def run_tool(self, name: str, tool_input: Any) -> tuple[str, bool]:
        """Run one tool call. Returns (content, is_error)."""
        if not isinstance(tool_input, dict):
            return json.dumps({"INVALID_JSON": json.dumps(tool_input)}), True
        if name == "list_reports":
            return self._list_reports(), False
        if name == "read_report":
            filename = tool_input.get("filename")
            if not isinstance(filename, str):
                return json.dumps({"INVALID_JSON": json.dumps(tool_input)}), True
            return self._read_report(filename)
        return f"Unknown tool: {name}", True

    def _list_reports(self) -> str:
        if not self.directory.is_dir():
            return "No reports have been saved yet."
        entries = []
        for path in sorted(self.directory.glob("*.md"), reverse=True):
            meta = _front_matter(path)
            entries.append(
                f"- {path.name} | {meta.get('date', 'unknown date')} | {meta.get('question', '')}"
            )
        return "\n".join(entries) or "No reports have been saved yet."

    def _read_report(self, filename: str) -> tuple[str, bool]:
        # `filename` is model output: only accept a bare .md name that resolves
        # inside the reports directory.
        root = self.directory.resolve()
        target = (root / filename).resolve()
        if Path(filename).name != filename or target.parent != root or target.suffix != ".md":
            return "Invalid file name. Use a name exactly as returned by list_reports.", True
        if not target.is_file():
            return f"No report named {filename}. Call list_reports to see what exists.", True
        return target.read_text(encoding="utf-8"), False


def _front_matter(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as f:
            if f.readline().strip() != "---":
                return meta
            for line in f:
                if line.strip() == "---":
                    break
                key, _, value = line.partition(":")
                value = value.strip()
                if value.startswith('"'):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass
                meta[key.strip()] = value
    except OSError:
        pass
    return meta
