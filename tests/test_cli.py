from __future__ import annotations

import builtins

import httpx2
import pytest

from research_agent import cli

from conftest import message


@pytest.fixture
def run(api, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.anthropic, "Anthropic", lambda: api.client())
    reports = tmp_path / "reports"

    def run(*args: str) -> int:
        return cli.main([*args, "--reports-dir", str(reports)])

    run.reports = reports
    return run


def text(s: str) -> dict:
    return {"type": "text", "text": s, "citations": None}


def test_one_shot_prints_answer_and_saves_report(api, run, capsys):
    api.queue(message([text("The answer.")], searches=1))

    assert run("What", "is", "it?", "--effort", "medium") == 0

    out = capsys.readouterr().out
    assert "The answer." in out
    assert "1 search, 0 pages read" in out
    (report,) = run.reports.glob("*.md")
    assert report.name.endswith("-what-is-it.md")
    assert "The answer." in report.read_text()
    assert api.requests[0]["output_config"] == {"effort": "medium"}


def test_no_save(api, run):
    api.queue(message([text("The answer.")]))

    assert run("Question", "--no-save") == 0
    assert not run.reports.exists()


def test_api_errors_exit_cleanly(api, run, capsys):
    api.queue(httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}))

    assert run("Question") == 1
    assert "API returned 500" in capsys.readouterr().err


def test_refusal_exits_cleanly(api, run, capsys):
    api.queue(message([], stop_reason="refusal", stop_details={"type": "refusal", "category": "bio", "explanation": None}))

    assert run("Question") == 1
    assert "declined (bio)" in capsys.readouterr().err


@pytest.fixture
def no_credentials(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


def test_missing_api_key(no_credentials, tmp_path, capsys):
    assert cli.main(["Question", "--reports-dir", str(tmp_path / "r")]) == 1
    assert "no API key found" in capsys.readouterr().err


def test_broken_profile_config(no_credentials, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "missing"))

    assert cli.main(["Question", "--reports-dir", str(tmp_path / "r")]) == 1
    assert "could not load credentials" in capsys.readouterr().err


def test_repl_follow_ups_and_new_conversation(api, run, monkeypatch, capsys):
    api.queue(message([text("One.")]), message([text("Two.")]), message([text("Three.")]))
    lines = iter(["First question", "Follow-up", "/new", "Fresh question", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(lines))

    assert run() == 0

    out = capsys.readouterr().out
    assert "One." in out and "Two." in out and "Three." in out
    assert "Started a new conversation." in out
    assert len(api.requests[1]["messages"]) == 3  # follow-up carries the first exchange
    assert len(api.requests[2]["messages"]) == 1  # /new cleared it
    assert len(list(run.reports.glob("*.md"))) == 3
