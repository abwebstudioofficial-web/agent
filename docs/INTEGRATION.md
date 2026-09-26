# Adding the research assistant to Logistix

This guide is for whoever adds the research assistant to the Logistix ERP site.
If you're a Claude session, follow these steps against the Logistix codebase.

## What you're adding

- A chat panel in the bottom-right corner of every Logistix page, for logged-in
  users.
- It researches anything about transportation and logistics on the web: freight
  rates, fuel prices, carriers, FMCSA/DOT rules, customs, ports, disruptions and
  industry news. It answers briefly and cites its sources. It runs on Claude
  Opus 5 and Anthropic's built-in web search and web fetch tools, so there is no
  search API or scraper to set up.
- It knows Logistix's pages and can take users to them. "Take me to shipment
  SH-1042" goes straight there. When a page is relevant to an answer (say, the
  Rates page after a diesel-price question), it shows a **Go to Rates & quotes →**
  button instead.
- The conversation carries on as the user moves between pages.

## How it fits together

```
Browser (every page)                 Logistix server                          Anthropic API
─────────────────────                ───────────────                          ─────────────
widget.js chat panel ── POST /api/research-agent/chat ──► research_agent.web ──► Claude + web tools
                     ◄── streamed events (SSE) ────────── (FastAPI, holds key)
```

- `research_agent/static/widget.js` is the panel. It's a plain script with no
  dependencies. It renders inside Shadow DOM, so the site's CSS and the panel's
  CSS can't affect each other.
- `research_agent/web.py` holds the endpoints, the per-user conversations, and
  the event stream.
- `research_agent/site.py` holds the site map, the `navigate_to` tool, and the
  Logistix-specific system prompt.
- `research_agent/agent.py` is the agent loop, shared with the command-line
  version.

The Anthropic API key stays on the server. The browser only talks to
`/api/research-agent/*` on the same origin as the site.

## Steps

### 1. Get the code into the project

Either install it as a package:

```bash
pip install "research-agent[web] @ git+https://github.com/abwebstudioofficial-web/agent.git"
```

or copy the `research_agent/` folder into the backend and install
`anthropic>=1.8,<2`, `fastapi>=0.110` and `uvicorn>=0.29`.

### 2. Write the site map from Logistix's real routes

Create a JSON file (for example `research_site_map.json`) that lists every page
users can be sent to. `research_agent/static/example_site_map.json` is a
starting point, but replace its paths with the ones in Logistix's router:

```json
[
  {"path": "/shipments", "title": "Shipments", "description": "search and filter all shipments, e.g. ?status=delayed"},
  {"path": "/shipments/{id}", "title": "Shipment details", "description": "tracking, stops, documents and charges for one shipment"},
  {"path": "/carriers", "title": "Carriers", "description": "carrier directory, onboarding, insurance and safety ratings"}
]
```

- Paths start with `/`. Use `{name}` for IDs. Claude fills these in only with
  values the user actually gave.
- Claude uses the `description` to decide which page fits, so say what the page
  is for.
- Claude can only navigate to paths that match this list, optionally with a
  query string. Anything else is rejected. Leave out pages that only some users
  can open, or have the page itself handle permissions, as it already should.

### 3. Serve the endpoints

**If the Logistix backend is FastAPI**, add the routes to the existing app:

```python
from research_agent.site import SiteMap
from research_agent.web import create_app

create_app(
    SiteMap.from_json("research_site_map.json"),
    site_name="Logistix",
    authorize=current_user_id,  # step 4
    app=app,                    # your existing FastAPI app
)
```

**Otherwise** (Flask, Django, Node/Next.js, PHP, .NET, anything), run it as a
small separate service and proxy to it from the main site:

```python
# research_service.py
from research_agent.site import SiteMap
from research_agent.web import create_app

app = create_app(
    SiteMap.from_json("research_site_map.json"),
    site_name="Logistix",
    authorize=current_user_id,  # step 4
)
```

```bash
ANTHROPIC_API_KEY=sk-ant-... uvicorn research_service:app --host 127.0.0.1 --port 8100 --workers 1
```

Then forward `/api/research-agent/` from the main site to
`http://127.0.0.1:8100/api/research-agent/`. Keeping it on the same origin means
the browser's login cookie reaches the service. The proxy must not buffer the
event stream, and it must allow long requests, because research can take a
minute or two:

```nginx
location /api/research-agent/ {
    proxy_pass http://127.0.0.1:8100;
    proxy_buffering off;
    proxy_read_timeout 600s;
    proxy_set_header Host $host;
    proxy_set_header Cookie $http_cookie;
}
```

For Next.js, a rewrite in `next.config.js` works:
`{ source: "/api/research-agent/:path*", destination: "http://127.0.0.1:8100/api/research-agent/:path*" }`.

Use `--workers 1`. Conversations are kept in memory, so with several worker
processes a follow-up question can land on a worker that has never seen the
conversation. To scale further, replace `SessionStore` in `web.py` with shared
storage.

### 4. Restrict it to logged-in users (required)

`authorize` receives the incoming FastAPI `Request`. It must return a stable ID
for the logged-in user, or `None` to reject the request with 401. The default
lets everyone in, and then anyone who finds the URL can spend the API credits.
Base it on however Logistix already authenticates requests, for example:

```python
import jwt  # if Logistix uses a signed JWT cookie

def current_user_id(request):
    token = request.cookies.get("logistix_session")
    if not token:
        return None
    try:
        claims = jwt.decode(token, LOGISTIX_JWT_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    return str(claims["sub"])
```

If sessions are stored server-side instead, look the cookie up in that store
(database, Redis, or an internal "who am I" endpoint on the main app). Each
conversation belongs to the user who started it, and no one else can continue
it.

It's also worth rate limiting `/api/research-agent/chat` per user (for example,
20 questions an hour) at the proxy or in `authorize`.

### 5. Add the widget to the site layout

Add this tag to the main layout, shown only to logged-in users:

```html
<script src="/api/research-agent/widget.js" defer></script>
```

Optional attributes: `data-title="Logistix Assistant"`, and `data-greeting="..."`
for the first message.

If Logistix is a single-page app, send navigation through its router so the page
doesn't fully reload:

```js
window.ResearchAgent = window.ResearchAgent || {};
window.ResearchAgent.navigate = (path) => router.push(path);  // React Router: navigate(path); Vue: router.push(path)
```

Other hooks: `window.ResearchAgent.open()`, `.close()` and `.ask("question")`.
For example, you could add an "Ask about this carrier" button on a carrier page.

### 6. Configure

- Set `ANTHROPIC_API_KEY` on the server only. Never put it in frontend code.
- `effort` in `create_app` defaults to `"medium"` for quick chat replies. Use
  `"high"` for deeper research at the cost of slower, more expensive answers.
- Refusal fallback is on. If Claude's safety filters decline a request, the API
  retries it on another Claude model. This occasionally happens on harmless
  topics such as hazmat.

### 7. Try it

Before touching the site, you can run a local demo that serves placeholder pages
for every path in the site map, with the widget on each one:

```bash
pip install -e '.[web]'
ANTHROPIC_API_KEY=sk-ant-... research-agent-web --site-map research_site_map.json
# open http://127.0.0.1:8000/dashboard
```

Questions to try:

- "What's the national average diesel price this week?"
- "What are the FMCSA hours-of-service limits for property carriers?"
- "Take me to shipment SH-1042"
- "Where do I update fuel surcharges?"

## Optional: answer questions from Logistix's own data

The assistant can also look things up in Logistix, such as a shipment's status
or a carrier's insurance expiry. Add a tool provider and pass it with
`tools_for_user`. The provider receives the user ID, so every lookup can be
limited to that user's company:

```python
class ShipmentLookup:
    def __init__(self, user_id: str):
        self.company_id = company_for_user(user_id)

    @property
    def tool_definitions(self):
        return [{
            "name": "lookup_shipment",
            "description": (
                "Look up one of the user's shipments by its ID (like SH-1042) and return its "
                "status, current location, stops and ETA. Call this when the user asks about "
                "a specific shipment."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"shipment_id": {"type": "string"}},
                "required": ["shipment_id"],
                "additionalProperties": False,
            },
            "eager_input_streaming": True,
        }]

    def run_tool(self, name, tool_input):
        shipment_id = tool_input.get("shipment_id") if isinstance(tool_input, dict) else None
        if not isinstance(shipment_id, str):
            return "shipment_id is required.", True
        shipment = db.find_shipment(shipment_id, company_id=self.company_id)  # scope every query
        if shipment is None:
            return f"No shipment {shipment_id} found.", True
        return format_shipment(shipment), False  # (result text, is_error)


create_app(site, site_name="Logistix", authorize=current_user_id,
           tools_for_user=lambda user_id: [ShipmentLookup(user_id)])
```

Keep tools read-only unless there's a confirmation step in the UI. Validate every
input: tool arguments come from the model, not from a trusted form.

## If you need to port it instead

Running the Python service behind a proxy (step 3) is the simplest option on any
stack. If it has to live inside a non-Python backend, port `agent.py`, `site.py`
and `web.py` using the official Anthropic SDK for that language, and keep the
event protocol below so `widget.js` works unchanged. Carry over these details:

- Model `claude-opus-5` with `thinking: {type: "adaptive"}` and
  `output_config: {effort: "medium"}`, streamed, `max_tokens: 64000`.
- Tools: `{"type": "web_search_20260209", "name": "web_search"}` and
  `{"type": "web_fetch_20260209", "name": "web_fetch"}` (server-side, nothing to
  implement), plus `navigate_to` with `eager_input_streaming: true`. Validate its
  input before use.
- Refusal fallback: send `fallbacks: "default"` with the beta header
  `server-side-fallback-2026-07-01`, on the beta messages endpoint.
- Top-level `cache_control: {type: "ephemeral"}` for prompt caching.
- On `stop_reason: "pause_turn"`, append the assistant content and send the
  request again with no new user message. On `"refusal"`, drop the turn.
- After a `fallback` block appears in a response, keep only text blocks and
  paired server-tool blocks from before the last one (`history_content()` in
  `agent.py`).
- The reply is the text after the last non-text block (`final_text()`).

### Event protocol

`POST /api/research-agent/chat` with JSON `{"session_id": string|null, "message": string}`
(1–4000 characters) responds with `text/event-stream`:

| event | data | meaning |
| --- | --- | --- |
| `session` | `{"session_id"}` | Always first. Send it back with follow-ups. |
| `status` | `{"kind", "text"}` | Progress, e.g. `"Searching: diesel prices"`. `kind` is one of `thinking`, `search`, `read`, `navigate`, `tool`, `error`, `fallback`, `notice`. |
| `text` | `{"delta"}` | Reply text as it streams, including brief notes between searches. |
| `done` | `{"answer", "sources": [{"url", "title"}], "navigate": {"path", "title", "go_now"} \| null, "truncated"}` | The final reply as Markdown. Replace the streamed text with it. |
| `error` | `{"message"}` | A message that is safe to show the user. |

Error responses: `401` when not logged in, `409` while the conversation is still
answering the previous message, and `422` for an empty or overlong message.

`POST /api/research-agent/reset` with `{"session_id"}` forgets a conversation.
`GET /api/research-agent/widget.js` serves the widget.
