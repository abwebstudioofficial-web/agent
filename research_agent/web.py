"""A web endpoint for the research agent, plus a drop-in chat widget.

    POST {prefix}/chat    {"session_id": "..." | null, "message": "..."}
        -> text/event-stream of these events (see QueueDisplay and _start_turn):
           session  {"session_id"}                  first event; send it back with follow-ups
           status   {"kind", "text"}                what the agent is doing right now
           text     {"delta"}                       streamed reply text
           done     {"answer", "sources", "navigate", "truncated"}
           error    {"message"}
    POST {prefix}/reset   {"session_id": "..."}     forget a conversation
    GET  {prefix}/widget.js                         the chat widget script

Your Anthropic API key stays on the server. Put the endpoint behind your site's
login by passing `authorize` (see create_app), or anyone who finds it can spend
your API credits.
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import urlparse

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .agent import AgentError, ResearchAgent, Refused, ToolProvider
from .site import SiteMap, SiteNavigator, build_site_prompt

log = logging.getLogger(__name__)

WIDGET_JS = Path(__file__).with_name("static") / "widget.js"
MAX_MESSAGE_CHARS = 4000


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ResetRequest(BaseModel):
    session_id: str


# -- progress events ------------------------------------------------------------


class QueueDisplay:
    """Turns the agent's progress callbacks into (event, data) pairs on a queue."""

    def __init__(self, events: queue.Queue):
        self.events = events

    def _status(self, kind: str, text: str) -> None:
        self.events.put(("status", {"kind": kind, "text": text}))

    def text(self, delta: str) -> None:
        self.events.put(("text", {"delta": delta}))

    def thinking(self, delta: str) -> None:
        pass

    def thinking_started(self) -> None:
        self._status("thinking", "Thinking…")

    def tool_call(self, name: str, tool_input: dict[str, Any]) -> None:
        if name == "web_search":
            self._status("search", f"Searching: {tool_input.get('query', '')}")
        elif name == "web_fetch":
            url = str(tool_input.get("url", ""))
            self._status("read", f"Reading: {urlparse(url).netloc or url}")
        elif name == "navigate_to":
            self._status("navigate", "Finding the right page…")
        else:
            self._status("tool", f"Using {name}…")

    def tool_error(self, name: str, error: str) -> None:
        self._status("error", f"{name} failed, trying another way…")

    def fallback(self, from_model: str, to_model: str) -> None:
        self._status("fallback", "Switching models to continue…")

    def notice(self, message: str) -> None:
        self._status("notice", message)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _user_message(error: BaseException) -> str:
    """A message that is safe to show end users; details go to the server log."""
    if isinstance(error, Refused):
        return "Sorry, I can't help with that request."
    if isinstance(error, AgentError):
        return f"Sorry, I couldn't finish that: {error}"
    if isinstance(error, anthropic.RateLimitError):
        return "The research assistant is busy right now. Please try again in a minute."
    if isinstance(error, (anthropic.APIConnectionError, anthropic.InternalServerError)):
        return "The research assistant couldn't reach its AI service. Please try again."
    return "Something went wrong. Please try again."


# -- sessions -------------------------------------------------------------------


@dataclass
class Session:
    owner: str
    agent: ResearchAgent
    navigator: SiteNavigator
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.monotonic)


class SessionStore:
    """In-memory conversations, evicted when idle or when there are too many.

    Conversations are lost when the server restarts, which is fine for a chat
    panel. Run a single worker process, or swap this for shared storage.
    """

    def __init__(self, make_session: Callable[[str], Session], max_sessions: int, ttl_seconds: float):
        self._make = make_session
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds

    def get_or_create(self, session_id: str | None, owner: str) -> tuple[str, Session]:
        with self._lock:
            self._evict()
            session = self._sessions.get(session_id) if session_id else None
            if session is None or session.owner != owner:
                session_id = secrets.token_urlsafe(18)
                session = self._make(owner)
                self._sessions[session_id] = session
            self._sessions.move_to_end(session_id)
            session.last_used = time.monotonic()
            return session_id, session

    def remove(self, session_id: str, owner: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.owner == owner:
                del self._sessions[session_id]

    def _evict(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        for key in [k for k, s in self._sessions.items() if s.last_used < cutoff]:
            del self._sessions[key]
        while len(self._sessions) >= self.max_sessions:
            self._sessions.popitem(last=False)


# -- the app --------------------------------------------------------------------


def create_app(
    site: SiteMap,
    *,
    site_name: str,
    authorize: Callable[[Request], str | None] = lambda request: "anonymous",
    tools_for_user: Callable[[str], Sequence[ToolProvider]] | None = None,
    client: anthropic.Anthropic | None = None,
    effort: str = "medium",
    prefix: str = "/api/research-agent",
    max_sessions: int = 1000,
    session_ttl_seconds: float = 3600,
    app: FastAPI | None = None,
) -> FastAPI:
    """Build (or extend) a FastAPI app with the research agent endpoints.

    authorize: given the incoming request, return a stable ID for the logged-in
        user, or None to reject it with 401. The default lets everyone in, so
        replace it with your site's session check before going live.
    tools_for_user: given that user ID, return extra tools for their conversation,
        e.g. a shipment lookup limited to their company's data.
    effort: "low" and "medium" keep chat replies quick; "high" researches harder.
    app: pass your existing FastAPI app to mount the routes on it.
    """
    app = app or FastAPI(title=f"{site_name} research assistant")
    client = client or anthropic.Anthropic()
    system = build_site_prompt(site_name, site)

    def make_session(owner: str) -> Session:
        navigator = SiteNavigator(site)
        extra = list(tools_for_user(owner)) if tools_for_user else []
        agent = ResearchAgent(client, tools=[navigator, *extra], system=system, effort=effort)
        return Session(owner=owner, agent=agent, navigator=navigator)

    store = SessionStore(make_session, max_sessions, session_ttl_seconds)

    def current_user(request: Request) -> str:
        user = authorize(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in to use the research assistant.")
        return user

    @app.post(f"{prefix}/chat")
    def chat(body: ChatRequest, request: Request):
        session_id, session = store.get_or_create(body.session_id, current_user(request))
        if not session.lock.acquire(blocking=False):
            return JSONResponse({"error": "Still working on your last message."}, status_code=409)
        events = _start_turn(session, body.message.strip())
        return StreamingResponse(
            _relay(session_id, events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(f"{prefix}/reset")
    def reset(body: ResetRequest, request: Request):
        store.remove(body.session_id, current_user(request))
        return {"ok": True}

    @app.get(f"{prefix}/widget.js")
    def widget():
        return FileResponse(WIDGET_JS, media_type="application/javascript")

    app.state.research_sessions = store
    return app


_DONE = object()


def _start_turn(session: Session, message: str) -> queue.Queue:
    """Answer one message in a worker thread; progress events arrive on the returned queue.

    The caller has already acquired session.lock; the worker always releases it,
    even if the browser disconnects before reading anything.
    """
    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            session.agent.display = QueueDisplay(events)
            session.navigator.destination = None
            answer = session.agent.ask(message)
            destination = session.navigator.take_destination()
            events.put((
                "done",
                {
                    "answer": answer.report,
                    "sources": [{"url": s.url, "title": s.title} for s in answer.sources],
                    "navigate": destination.to_dict() if destination else None,
                    "truncated": answer.truncated,
                },
            ))
        except Exception as error:  # reported to the user; details logged here
            log.exception("research agent turn failed")
            events.put(("error", {"message": _user_message(error)}))
        finally:
            session.last_used = time.monotonic()
            session.lock.release()
            events.put(_DONE)

    threading.Thread(target=work, daemon=True).start()
    return events


def _relay(session_id: str, events: queue.Queue) -> Iterator[str]:
    yield _sse("session", {"session_id": session_id})
    while (item := events.get()) is not _DONE:
        event, data = item
        yield _sse(event, data)


# -- demo server ------------------------------------------------------------------

EXAMPLE_SITE_MAP = Path(__file__).with_name("static") / "example_site_map.json"

_DEMO_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {site_name} demo</title>
<style>
body{{margin:0;font:15px/1.5 system-ui,sans-serif;background:#f6f7f9;color:#1c2230}}
nav{{display:flex;flex-wrap:wrap;gap:4px 14px;padding:12px 20px;background:#1c2230}}
nav a{{color:#cfd6e4;text-decoration:none}} nav a:hover{{color:#fff}}
main{{padding:24px 20px;max-width:760px}}
@media (prefers-color-scheme:dark){{body{{background:#10131a;color:#e8ebf2}}}}
</style></head>
<body><nav>{links}</nav>
<main><h1>{title}</h1><p>Demo page for <code>{path}</code>. In your real site this is where the
{title} screen would be. Open the research assistant in the corner to try it.</p></main>
<script src="{prefix}/widget.js" defer></script>
</body></html>"""


def build_demo_app(
    site: SiteMap, *, site_name: str, client: anthropic.Anthropic | None = None, effort: str = "medium"
) -> FastAPI:
    """The research agent endpoints plus placeholder pages for every site-map path."""
    import html

    from fastapi.responses import HTMLResponse

    app = create_app(site, site_name=site_name, client=client, effort=effort)
    prefix = "/api/research-agent"
    links = "".join(
        f'<a href="{html.escape(p.path)}">{html.escape(p.title)}</a>' for p in site.pages if "{" not in p.path
    )

    @app.get("/{path:path}", response_class=HTMLResponse)
    def demo_page(path: str):
        path = "/" + path
        page = site.find(path)
        if page is None and path != "/":
            raise HTTPException(status_code=404)
        return _DEMO_PAGE.format(
            title=html.escape(page.title if page else "Home"),
            site_name=html.escape(site_name),
            links=links,
            path=html.escape(path),
            prefix=prefix,
        )

    return app


def main(argv: list[str] | None = None) -> int:
    """Run a local demo: placeholder site pages with the widget on every one."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="research-agent-web", description=main.__doc__)
    parser.add_argument("--site-map", type=Path, default=EXAMPLE_SITE_MAP, help="JSON site map (default: example ERP pages).")
    parser.add_argument("--site-name", default="Logistix", help="Name the assistant uses for the site.")
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    app = build_demo_app(SiteMap.from_json(args.site_map), site_name=args.site_name, effort=args.effort)
    logging.basicConfig(level=logging.INFO)
    print(f"Demo running at http://{args.host}:{args.port}/dashboard")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0
