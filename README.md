# Research Agent

A command-line research agent powered by Claude. Ask it a question and it
searches the web, reads the most useful pages, and writes a sourced report.
Each report is saved as a Markdown file you can come back to.

```
$ research-agent "What's the current state of solid-state batteries for EVs?"
  · thinking
  → searching: solid-state battery EV production 2026
  → reading:   https://www.example.com/solid-state-battery-update
  → searching: solid-state battery energy density cost comparison
  · thinking

**Short answer:** ...

## Sources
- ...

saved to reports/2026-09-25-what-s-the-current-state-of-solid-state.md · 4 searches, 3 pages read · 61,204 input / 2,310 output tokens
```

## Setup

You need Python 3.10+ and an Anthropic API key, which you can create in the
[Claude Console](https://platform.claude.com).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

export ANTHROPIC_API_KEY=sk-ant-...
```

If you've already signed in with the `ant auth login` CLI, the SDK picks up
that login and you can skip the export.

## Usage

Ask a single question:

```bash
research-agent "How do mRNA vaccines work?"
```

Or start an interactive session and ask follow-up questions. The agent keeps
the whole conversation in mind:

```bash
research-agent
research> Compare the top three vector databases for a small startup
research> Which of those is cheapest to self-host?
research> /new          # start a fresh conversation
research> /exit         # or Ctrl-D
```

Press Ctrl-C while it's working to cancel the current question.

| Option | What it does |
| --- | --- |
| `--effort low\|medium\|high\|xhigh\|max` | How hard Claude works on each question. `high` is the default. `low` and `medium` are faster and cheaper, and they're often enough for simple factual questions. |
| `--reports-dir PATH` | Where reports are saved and read back from. The default is `./reports`. |
| `--no-save` | Don't write reports to disk. |
| `--show-thinking` | Print summaries of Claude's reasoning as it works. |

You can also run it with `python -m research_agent`.

### Saved reports

Each answer is saved to `reports/<date>-<question>.md`. The file has the
question, date and model in its front matter, then the report, then a
**Pages consulted** list. That list comes from the pages the agent actually
fetched and the search results it cited, so you can check the report's own
Sources section against it.

The agent can also read its past reports. Ask *"what have I researched about
batteries before?"* or *"update last week's report on X"* and it will look them
up.

## How it works

| File | Role |
| --- | --- |
| `research_agent/agent.py` | The agent loop. It streams each Claude response, resumes paused turns, runs local tools, and keeps the conversation history. |
| `research_agent/prompts.py` | The system prompt: how to research and how to write the answer. |
| `research_agent/reports.py` | Saves reports, plus the `list_reports` and `read_report` tools Claude uses to read them back. |
| `research_agent/cli.py` | The terminal interface. |

A few details:

- **Model:** Claude Opus 5 (`claude-opus-5`) with adaptive thinking.
- **Web tools:** web search and web fetch are
  [server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview).
  Anthropic runs them, so there is no scraping code or search API key to manage.
  When a long research turn hits the server's tool-iteration limit
  (`pause_turn`), the loop picks it up again automatically.
- **Refusal fallback:** requests are sent with `fallbacks: "default"`. If
  Claude's safety classifiers decline a request (this occasionally happens on
  harmless security or life-sciences topics), the API retries it on a fallback
  model within the same call. To turn this off, remove the `betas` and
  `fallbacks` arguments in `ResearchAgent._stream_once`.
- **Prompt caching** is on, so follow-up questions don't pay full price to
  re-read the conversation so far.
- **Safety limits:** each question stops after 40 API calls. `read_report` can
  only open `.md` files inside the reports directory.

## Customizing

**Change how it researches or writes.** Edit `SYSTEM_PROMPT` in
`research_agent/prompts.py`. For example, you can ask for a fixed report
template, a different length, or a particular audience.

**Restrict which sites it uses.** Add `allowed_domains` or `blocked_domains` to
the tools in `SERVER_TOOLS` in `agent.py` (use one or the other, not both):

```python
SERVER_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "allowed_domains": ["arxiv.org", "nature.com"]},
    {"type": "web_fetch_20260209", "name": "web_fetch", "allowed_domains": ["arxiv.org", "nature.com"]},
]
```

You can also cap the cost of each request with `"max_uses": 5`.

**Add your own tool** (for example, querying an internal database):

1. Add the tool's definition to `self.tools` in `ResearchAgent.__init__`. It
   needs a `name`, a `description` that says *when* Claude should call it, an
   `input_schema`, and `"eager_input_streaming": True`. The entries in
   `ReportLibrary.tool_definitions` are examples.
2. In `ResearchAgent._run_tools`, route calls to that tool name to your code.
   Right now every call goes to `ReportLibrary.run_tool`. Validate the input
   before you use it, and return `(result_text, is_error)`.

**Use a different model.** Pass `model=` to `ResearchAgent`, or change
`DEFAULT_MODEL` in `agent.py`. Older models may not accept the `fallbacks`
parameter or the `_20260209` web tool versions, so adjust those too.

## Costs

Every question uses Claude tokens, and web searches are billed per search on
top of that. Claude Opus 5 is $5 per million input tokens and $25 per million
output tokens. Cached input is much cheaper. A typical question takes a few
searches and tens of thousands of input tokens, because the pages the agent
reads count as input. The summary line after each answer shows what that
question used. See <https://platform.claude.com/docs/en/about-claude/pricing>
for current prices.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests don't need an API key or network access. They run the real Anthropic
SDK against a fake HTTP transport that replays scripted streaming responses
(see `tests/conftest.py`).
