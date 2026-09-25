from __future__ import annotations

from research_agent.reports import ReportLibrary, Source, slugify


def test_save_writes_front_matter_report_and_sources(tmp_path):
    library = ReportLibrary(tmp_path / "reports")

    path = library.save(
        'What is "RAG": a primer?',
        "## Answer\n\nIt's retrieval.\n",
        [Source("https://a.example", "A"), Source("https://b.example")],
        "claude-opus-5",
    )

    body = path.read_text()
    assert path.name.endswith("-what-is-rag-a-primer.md")
    assert body.startswith('---\nquestion: "What is \\"RAG\\": a primer?"\n')
    assert "model: claude-opus-5\n---\n\n## Answer\n\nIt's retrieval.\n" in body
    assert "- [A](https://a.example)\n- [https://b.example](https://b.example)\n" in body


def test_save_never_overwrites(tmp_path):
    library = ReportLibrary(tmp_path)
    first = library.save("Same question", "one", [], "m")
    second = library.save("Same question", "two", [], "m")

    assert first != second
    assert second.stem.endswith("-2")
    assert "one" in first.read_text()


def test_list_and_read_round_trip(tmp_path):
    library = ReportLibrary(tmp_path)
    path = library.save('Question with "quotes"', "Body text.", [], "m")

    listing, is_error = library.run_tool("list_reports", {})
    assert not is_error
    assert path.name in listing
    assert 'Question with "quotes"' in listing

    content, is_error = library.run_tool("read_report", {"filename": path.name})
    assert not is_error
    assert "Body text." in content


def test_list_with_no_reports(tmp_path):
    library = ReportLibrary(tmp_path / "missing")
    assert library.run_tool("list_reports", {}) == ("No reports have been saved yet.", False)


def test_read_report_rejects_paths_outside_the_directory(tmp_path):
    reports = tmp_path / "reports"
    library = ReportLibrary(reports)
    library.save("q", "body", [], "m")
    (tmp_path / "secret.md").write_text("secret")

    for bad in ["../secret.md", "/etc/passwd", "sub/../../secret.md", "notes.txt", ".."]:
        content, is_error = library.run_tool("read_report", {"filename": bad})
        assert is_error, bad
        assert "secret" not in content


def test_read_report_validates_input(tmp_path):
    library = ReportLibrary(tmp_path)
    assert library.run_tool("read_report", {})[1] is True
    assert library.run_tool("read_report", {"filename": 3})[1] is True
    assert library.run_tool("read_report", "nope")[1] is True
    assert library.run_tool("read_report", {"filename": "missing.md"})[1] is True
    assert library.run_tool("delete_everything", {})[1] is True


def test_slugify():
    assert slugify("How do mRNA vaccines work, in 2026?") == "how-do-mrna-vaccines-work-in-2026"
    assert slugify("???") == "report"
