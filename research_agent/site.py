"""Running the agent inside a website: a site map, a navigation tool, and a site-aware prompt.

The site map is a list of the pages users can be sent to. Paths can contain
{placeholders} for IDs, e.g. "/shipments/{id}". Claude sees the list in its system
prompt and calls `navigate_to` when a page is relevant; the web front end then
shows a button to that page, or goes there straight away when the user asked to.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Characters allowed in a {placeholder} value and in a query string.
_SEGMENT = r"[A-Za-z0-9_\-.~%]+"
_QUERY = re.compile(r"[A-Za-z0-9_\-.~%=&+,]*")


@dataclass(frozen=True)
class Page:
    path: str  # "/shipments" or a template such as "/shipments/{id}"
    title: str
    description: str = ""

    def matches(self, path: str) -> bool:
        pattern = re.sub(r"\\\{[A-Za-z0-9_]+\\\}", lambda _: _SEGMENT, re.escape(self.path))
        return re.fullmatch(pattern, path) is not None


@dataclass(frozen=True)
class Destination:
    path: str
    title: str
    go_now: bool

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "title": self.title, "go_now": self.go_now}


class SiteMap:
    def __init__(self, pages: list[Page]):
        if not pages:
            raise ValueError("The site map needs at least one page.")
        for page in pages:
            if not page.path.startswith("/") or page.path.startswith("//"):
                raise ValueError(f"Page paths must start with a single '/': {page.path!r}")
        self.pages = pages

    @classmethod
    def from_json(cls, path: Path) -> SiteMap:
        """Load a JSON file: [{"path": ..., "title": ..., "description": ...}, ...]."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([Page(p["path"], p["title"], p.get("description", "")) for p in data])

    def find(self, path: str) -> Page | None:
        """The page a path belongs to, or None if it isn't on the site map.

        Only same-site paths are accepted ("/fleet", "/shipments/SH-1042?tab=docs"),
        never full URLs or protocol-relative "//host" links.
        """
        if not path.startswith("/") or path.startswith("//"):
            return None
        base, _, query = path.partition("?")
        if not _QUERY.fullmatch(query):
            return None
        return next((page for page in self.pages if page.matches(base)), None)

    def prompt_section(self) -> str:
        lines = []
        for page in self.pages:
            line = f"- {page.path} — {page.title}"
            if page.description:
                line += f": {page.description}"
            lines.append(line)
        return "\n".join(lines)


class SiteNavigator:
    """The `navigate_to` tool. Keep one per conversation: it remembers where to go."""

    def __init__(self, site: SiteMap):
        self.site = site
        self.destination: Destination | None = None

    @property
    def tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "navigate_to",
                "description": (
                    "Take the user to a page on this site. Call it when the user asks to go "
                    "somewhere, or when one page on the site is clearly where they would act "
                    "on your answer. Use only paths from the site map in your instructions, "
                    "and call it at most once per reply."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "A path from the site map, with any {placeholders} filled in from "
                                "values the user gave, e.g. /shipments/SH-1042. May end in a query "
                                "string such as ?status=delayed."
                            ),
                        },
                        "go_now": {
                            "type": "boolean",
                            "description": (
                                "true only if the user asked to be taken there (\"take me to...\", "
                                "\"open...\"). false shows them a button to the page instead."
                            ),
                        },
                    },
                    "required": ["path", "go_now"],
                    "additionalProperties": False,
                },
                "eager_input_streaming": True,
            }
        ]

    def run_tool(self, name: str, tool_input: Any) -> tuple[str, bool]:
        if name != "navigate_to":
            return f"Unknown tool: {name}", True
        if not (
            isinstance(tool_input, dict)
            and isinstance(tool_input.get("path"), str)
            and isinstance(tool_input.get("go_now"), bool)
        ):
            return json.dumps({"INVALID_JSON": json.dumps(tool_input)}), True

        path = tool_input["path"].strip()
        page = self.site.find(path)
        if page is None:
            return f"{path!r} is not a page on this site. Use a path from the site map.", True

        self.destination = Destination(path, page.title, tool_input["go_now"])
        if self.destination.go_now:
            return f"The user will be taken to {page.title} ({path}) when your reply finishes.", False
        return f"The user will see a button to {page.title} ({path}) under your reply.", False

    def take_destination(self) -> Destination | None:
        """Where the last reply wants to send the user; clears it for the next reply."""
        destination, self.destination = self.destination, None
        return destination


SITE_PROMPT = """\
You are the research assistant built into {site_name}, a transportation and \
logistics platform. You appear in a chat panel on every page and help its users \
with two things:

1. Researching anything related to transportation and logistics on the web: \
freight and spot rates, fuel prices, carriers and brokers, regulations and \
compliance (FMCSA, DOT, hours of service, customs, hazmat), ports, routes and \
border crossings, weather and disruptions, equipment, and industry news.
2. Finding their way around {site_name}: when a page on the site is where the \
user wants to go, or where they would act on your answer, take them there with \
navigate_to.

How to research:
- Use web_search to find sources and web_fetch to read the most promising pages \
in full. Prefer primary sources: regulators, carriers' and ports' own sites, \
official indexes and filings.
- Keep going until you can answer with confidence, and stop once more \
searching would not change the answer.
- Skip web research when the user only wants to go somewhere in {site_name} or \
is asking how to use it.
- Between tool calls, keep any notes to the user to one short sentence.

Taking the user to pages:
- Only use paths from the site map below. Fill in {{placeholders}} only with \
values the user gave you, such as a shipment number; never invent IDs.
- Set go_now to true only when the user asked to be taken somewhere. Otherwise \
set it to false and they will see a button to the page.

How to answer:
- The chat panel is narrow. Open with the answer in a sentence or two, then key \
details as a short list. Stay under about 300 words unless the user asks for \
more depth.
- Cite sources inline as Markdown links; the panel lists every page you read \
separately.
- Say plainly when sources disagree or information may be out of date. Do not \
guess.
- Stay focused on transportation, logistics, and using {site_name}. If asked \
about something unrelated, say briefly that you are set up for transportation \
and logistics research.

Site map of {site_name}:
{site_map}

Today's date is {today}."""


def build_site_prompt(site_name: str, site: SiteMap, today: dt.date | None = None) -> str:
    return SITE_PROMPT.format(
        site_name=site_name,
        site_map=site.prompt_section(),
        today=(today or dt.date.today()).isoformat(),
    )
