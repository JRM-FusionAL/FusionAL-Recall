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
        assert derive_si_id("abc-def", "SI-042 broke again") == "SI-042"

    def test_body_mention_does_not_hijack_identity(self):
        """A page whose *body* references SI-007 must NOT overwrite local SI-007."""
        page = _page(page_id="abcd1234-0000", title="broke", solution="Fix: see SI-007 for context")
        issue = map_page_to_issue(page)
        assert issue.si_id.startswith("N-")
        assert "SI-007" not in issue.si_id

    def test_page_id_fallback_uses_full_id(self):
        got = derive_si_id("396ac8f9-e6ff-8111-9434-deef3679b35b", "broke")
        assert got == "N-396ac8f9e6ff81119434deef3679b35b"

    def test_shared_prefix_pages_get_distinct_ids(self):
        """Notion page IDs are time-ordered — first-8-hex prefixes collide."""
        a = derive_si_id("396ac8f9-e6ff-8111-9434-deef3679b35b", "x")
        b = derive_si_id("396ac8f9-aaaa-8111-9434-000000000000", "y")
        assert a != b


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
        assert issue.si_id == "N-396ac8f9e6ff81119434deef3679b35b"
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


from unittest.mock import MagicMock, patch

import pytest

from recall.notion_sync import (
    NotionClient,
    NotionSyncError,
    build_notion_properties,
    sync_from_notion,
)


def _mock_response(status_code: int = 200, payload: dict | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = str(payload)
    return resp


class TestNotionClient:
    def test_query_paginates(self):
        client = NotionClient("tok", "ds-id")
        pages = [
            _mock_response(200, {"results": [{"id": "p1"}], "has_more": True, "next_cursor": "c1"}),
            _mock_response(200, {"results": [{"id": "p2"}], "has_more": False}),
        ]
        with patch("httpx.post", side_effect=pages) as post:
            got = client.query_all_pages()
        assert [p["id"] for p in got] == ["p1", "p2"]
        assert post.call_args_list[1].kwargs["json"]["start_cursor"] == "c1"

    def test_data_source_404_falls_back_to_database_endpoint(self):
        client = NotionClient("tok", "ds-id")
        responses = [
            _mock_response(404, {}),
            _mock_response(200, {"results": [], "has_more": False}),
        ]
        with patch("httpx.post", side_effect=responses) as post:
            client.query_all_pages()
        urls = [c.args[0] for c in post.call_args_list]
        assert "/data_sources/ds-id/query" in urls[0]
        assert "/databases/ds-id/query" in urls[1]

    def test_error_raises_notion_sync_error(self):
        client = NotionClient("tok", "ds-id")
        with patch("httpx.post", return_value=_mock_response(401, {})):
            with pytest.raises(NotionSyncError):
                client.query_all_pages()

    def test_create_page_returns_id(self):
        client = NotionClient("tok", "ds-id")
        with patch("httpx.post", return_value=_mock_response(200, {"id": "new-page"})):
            assert client.create_page({"Issue": {}}) == "new-page"


class TestBuildNotionProperties:
    def test_shape(self):
        props = build_notion_properties("t", "s", "FusionAL", ["python", "mcp"])
        assert props["Issue"]["title"][0]["text"]["content"] == "t"
        assert props["Tags"]["multi_select"] == [{"name": "python"}, {"name": "mcp"}]
        assert props["Severity"]["select"]["name"] == "Medium"
        assert props["Project"]["select"]["name"] == "FusionAL"

    def test_empty_project_defaults_general(self):
        assert build_notion_properties("t", "s", "", [])["Project"]["select"]["name"] == "General"

    def test_long_solution_chunked_under_2000(self):
        props = build_notion_properties("t", "x" * 4500, "General", [])
        chunks = props["Solution"]["rich_text"]
        assert len(chunks) == 3
        assert all(len(c["text"]["content"]) <= 2000 for c in chunks)


class TestSyncFromNotion:
    def _engine(self):
        engine = MagicMock()
        engine.embed.return_value = b"\x00" * 16
        return engine

    def test_inserts_new_pages(self, tmp_path):
        from recall.db import RecallDB

        db = RecallDB(tmp_path / "t.db")
        client = MagicMock()
        client.query_all_pages.return_value = [_page(page_id="aaaa-1"), _page(page_id="bbbb-2")]
        assert sync_from_notion(db, self._engine(), client) == 2
        assert db.count() == 2
        db.close()

    def test_unchanged_pages_skipped(self, tmp_path):
        from recall.db import RecallDB

        db = RecallDB(tmp_path / "t.db")
        client = MagicMock()
        client.query_all_pages.return_value = [_page(page_id="aaaa-1")]
        engine = self._engine()
        assert sync_from_notion(db, engine, client) == 1
        assert sync_from_notion(db, engine, client) == 0
        assert engine.embed.call_count == 1
        assert db.count() == 1
        db.close()

    def test_edited_page_reembedded(self, tmp_path):
        from recall.db import RecallDB

        db = RecallDB(tmp_path / "t.db")
        client = MagicMock()
        client.query_all_pages.return_value = [_page(page_id="aaaa-1")]
        engine = self._engine()
        sync_from_notion(db, engine, client)
        page2 = _page(page_id="aaaa-1", title="edited title")
        page2["last_edited_time"] = "2026-07-08T00:00:00.000Z"
        client.query_all_pages.return_value = [page2]
        assert sync_from_notion(db, engine, client) == 1
        assert db.count() == 1
        assert engine.embed.call_count == 2
        db.close()

    def test_notion_down_returns_zero(self, tmp_path):
        from recall.db import RecallDB

        db = RecallDB(tmp_path / "t.db")
        client = MagicMock()
        client.query_all_pages.side_effect = NotionSyncError("boom")
        assert sync_from_notion(db, self._engine(), client) == 0
        db.close()

    def test_titleless_page_skipped(self, tmp_path):
        from recall.db import RecallDB

        db = RecallDB(tmp_path / "t.db")
        bad = _page(page_id="cccc-3")
        bad["properties"]["Issue"]["title"] = []
        client = MagicMock()
        client.query_all_pages.return_value = [bad, _page(page_id="dddd-4")]
        assert sync_from_notion(db, self._engine(), client) == 1
        db.close()


class TestRememberDualWrite:
    def _setup(self, tmp_path, monkeypatch):
        from recall import server

        monkeypatch.setattr(server, "_db", None)
        monkeypatch.setattr(server, "_engine", None)
        monkeypatch.setattr(server, "_notion", None)
        monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "t.db"))
        engine = MagicMock()
        engine.embed.return_value = b"\x00" * 16
        monkeypatch.setattr(server, "get_engine", lambda: engine)
        return server

    def test_notion_success_stores_page_id(self, tmp_path, monkeypatch):
        server = self._setup(tmp_path, monkeypatch)
        client = MagicMock()
        client.create_page.return_value = "page-123"
        monkeypatch.setattr(server, "get_notion", lambda: client)
        result = server.remember(title="t", symptoms="s", root_cause="r", fix="f")
        assert result["notion_synced"] is True
        stored = server.get_db().get_issue_by_id(result["si_id"])
        assert stored.notion_page_id == "page-123"
        sent = client.create_page.call_args.args[0]
        assert "Symptoms: s" in sent["Solution"]["rich_text"][0]["text"]["content"]

    def test_notion_failure_still_writes_locally(self, tmp_path, monkeypatch):
        server = self._setup(tmp_path, monkeypatch)
        client = MagicMock()
        client.create_page.side_effect = NotionSyncError("down")
        monkeypatch.setattr(server, "get_notion", lambda: client)
        result = server.remember(title="t", symptoms="s", root_cause="r", fix="f")
        assert result["notion_synced"] is False
        assert server.get_db().get_issue_by_id(result["si_id"]) is not None

    def test_no_token_behaves_as_before(self, tmp_path, monkeypatch):
        server = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(server, "get_notion", lambda: None)
        result = server.remember(title="t", symptoms="s", root_cause="r", fix="f")
        assert result["notion_synced"] is False


class TestStartupSync:
    def test_startup_runs_notion_sync_when_configured(self, tmp_path, monkeypatch):
        from recall import server

        monkeypatch.setattr(server, "_db", None)
        monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "t.db"))
        monkeypatch.setattr(server, "SOLVED_ISSUES_PATH", None)
        engine = MagicMock()
        engine.embed.return_value = b"\x00" * 16
        monkeypatch.setattr(server, "get_engine", lambda: engine)
        client = MagicMock()
        client.query_all_pages.return_value = [_page(page_id="eeee-5")]
        monkeypatch.setattr(server, "get_notion", lambda: client)
        server._on_startup()
        assert server.get_db().count() == 1

    def test_startup_survives_notion_down(self, tmp_path, monkeypatch):
        from recall import server

        monkeypatch.setattr(server, "_db", None)
        monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "t.db"))
        monkeypatch.setattr(server, "SOLVED_ISSUES_PATH", None)
        monkeypatch.setattr(server, "get_engine", lambda: MagicMock())
        client = MagicMock()
        client.query_all_pages.side_effect = NotionSyncError("down")
        monkeypatch.setattr(server, "get_notion", lambda: client)
        server._on_startup()  # must not raise
        assert server.get_db().count() == 0
