"""Tests for recall.notion_sync — parsing, mapping, sync, client (all HTTP mocked)."""

from __future__ import annotations

from recall.notion_sync import derive_si_id, map_page_to_issue, parse_solution


def _rt(content: str) -> list[dict]:
    return [{"plain_text": content}]


def _page(
    page_id: str = "396ac8f9-e6ff-8111-9434-deef3679b35b",
    title: str = "Example issue",
    solution: str = "Symptoms: it broke\nRoot cause: bad config\nFix: fix the config",
    project: str = "FusionAL-Recall",
    tags: list[str] | None = None,
    severity: str | None = "High",
) -> dict:
    props: dict = {
        "Issue": {"title": _rt(title)},
        "Solution": {"rich_text": _rt(solution)},
        "Project": {"select": {"name": project}},
        "Tags": {"multi_select": [{"name": t} for t in (tags or ["python"])]},
        "Severity": {"select": {"name": severity} if severity else None},
    }
    return {
        "id": page_id,
        "created_time": "2026-07-07T12:00:00.000Z",
        "last_edited_time": "2026-07-07T12:30:00.000Z",
        "properties": props,
    }


class TestParseSolution:
    def test_labeled_newlines(self):
        parts = parse_solution("Symptoms: A\nRoot cause: B\nFix: C")
        assert parts == {"symptoms": "A", "root_cause": "B", "fix": "C"}

    def test_labeled_inline_no_newlines(self):
        parts = parse_solution("Symptoms: it hung. Root cause: (1) bad venv. Fix: reinstall deps.")
        assert parts["symptoms"] == "it hung."
        assert parts["root_cause"] == "(1) bad venv."
        assert parts["fix"] == "reinstall deps."

    def test_case_insensitive(self):
        parts = parse_solution("SYMPTOMS: a ROOT CAUSE: b FIX: c")
        assert parts["root_cause"] == "b"

    def test_unlabeled_goes_to_fix(self):
        parts = parse_solution("just restart the service")
        assert parts == {"symptoms": "", "root_cause": "", "fix": "just restart the service"}

    def test_empty(self):
        assert parse_solution("") == {"symptoms": "", "root_cause": "", "fix": ""}


class TestDeriveSiId:
    def test_legacy_id_in_title_reused(self):
        assert derive_si_id("abc-def", "SI-042 broke again", "") == "SI-042"

    def test_legacy_id_in_solution_reused(self):
        assert derive_si_id("abc-def", "broke", "see SI-007 for context") == "SI-007"

    def test_page_id_fallback(self):
        got = derive_si_id("396ac8f9-e6ff-8111-9434-deef3679b35b", "broke", "no legacy id")
        assert got == "N-396ac8f9"


class TestMapPageToIssue:
    def test_full_mapping(self):
        issue = map_page_to_issue(_page())
        assert issue is not None
        assert issue.title == "Example issue"
        assert issue.symptoms == "it broke"
        assert issue.root_cause == "bad config"
        assert issue.fix == "fix the config"
        assert issue.source == "FusionAL-Recall"
        assert "python" in issue.tags
        assert "severity:High" in issue.tags
        assert issue.notion_page_id == "396ac8f9-e6ff-8111-9434-deef3679b35b"
        assert issue.notion_edited_at == "2026-07-07T12:30:00.000Z"
        assert issue.si_id == "N-396ac8f9"
        assert issue.created_at.year == 2026

    def test_missing_title_returns_none(self):
        page = _page()
        page["properties"]["Issue"]["title"] = []
        assert map_page_to_issue(page) is None

    def test_null_selects_tolerated(self):
        page = _page(severity=None)
        page["properties"]["Project"]["select"] = None
        issue = map_page_to_issue(page)
        assert issue is not None
        assert issue.source == ""
        assert not any(t.startswith("severity:") for t in issue.tags)
