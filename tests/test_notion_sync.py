"""Tests for recall.notion_sync — parsing, mapping, sync, client (all HTTP mocked)."""

from __future__ import annotations

from datetime import datetime, timezone

from recall.models import Issue
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
        created = {"id": "new-page", "last_edited_time": "2026-07-07T12:30:00.000Z"}
        with patch("httpx.post", return_value=_mock_response(200, created)):
            assert client.create_page({"Issue": {}}) == ("new-page", "2026-07-07T12:30:00.000Z")


class TestBuildNotionProperties:
    def test_shape(self):
        props = build_notion_properties("t", "s", "FusionAL", ["python", "mcp"])
        assert props["Issue"]["title"][0]["text"]["content"] == "t"
        assert props["Tags"]["multi_select"] == [{"name": "python"}, {"name": "mcp"}]
        assert props["Severity"]["select"]["name"] == "Medium"
        assert props["Project"]["select"]["name"] == "FusionAL"

    def test_empty_project_defaults_general(self):
        assert build_notion_properties("t", "s", "", [])["Project"]["select"]["name"] == "General"

    def test_commas_scrubbed_from_select_options(self):
        # Notion 400s on a comma in a select/multi-select option name, and
        # `remember` passes its free-text `source` straight into Project. A
        # single comma used to fail the whole page create -- and the legacy
        # fallback then masked it as a 404, so two entries silently never
        # reached Notion.
        props = build_notion_properties(
            "t", "s", "~/Projects/clone -- review, 2026-08-16", ["a,b", "  ", "ok"]
        )

        assert "," not in props["Project"]["select"]["name"]
        assert props["Tags"]["multi_select"] == [{"name": "a b"}, {"name": "ok"}]

    def test_oversized_project_capped_to_notion_limit(self):
        props = build_notion_properties("t", "s", "P" * 300, [])

        assert len(props["Project"]["select"]["name"]) == 100

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


class TestRememberLocalFirst:
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

    def test_remember_is_local_first_and_marks_sync_pending(self, tmp_path, monkeypatch):
        server = self._setup(tmp_path, monkeypatch)
        client = MagicMock()
        monkeypatch.setattr(server, "get_notion", lambda: client)
        result = server.remember(title="t", symptoms="s", root_cause="r", fix="f")
        assert result["notion_sync"] == "pending"
        stored = server.get_db().get_issue_by_id(result["si_id"])
        assert stored.notion_page_id is None
        assert stored.notion_sync_pending is True
        client.create_page.assert_not_called()

    def test_sync_pushes_pending_local_issue(self, tmp_path, monkeypatch):
        server = self._setup(tmp_path, monkeypatch)
        client = MagicMock()
        client.create_page.return_value = ("page-123", "2026-07-07T12:30:00.000Z")
        monkeypatch.setattr(server, "get_notion", lambda: client)
        result = server.remember(title="t", symptoms="s", root_cause="r", fix="f")
        from recall.notion_sync import sync_to_notion
        assert sync_to_notion(server.get_db(), client) == 1
        stored = server.get_db().get_issue_by_id(result["si_id"])
        assert stored.notion_page_id == "page-123"
        assert stored.notion_sync_pending is False
        sent = client.create_page.call_args.args[0]
        assert "Symptoms: s" in sent["Solution"]["rich_text"][0]["text"]["content"]

    def test_notion_failure_still_writes_locally(self, tmp_path, monkeypatch):
        server = self._setup(tmp_path, monkeypatch)
        client = MagicMock()
        client.create_page.side_effect = NotionSyncError("down")
        monkeypatch.setattr(server, "get_notion", lambda: client)
        result = server.remember(title="t", symptoms="s", root_cause="r", fix="f")
        assert result["notion_sync"] == "pending"
        assert server.get_db().get_issue_by_id(result["si_id"]) is not None

    def test_no_token_behaves_as_before(self, tmp_path, monkeypatch):
        server = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(server, "get_notion", lambda: None)
        result = server.remember(title="t", symptoms="s", root_cause="r", fix="f")
        assert result["notion_sync"] == "pending"


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


def test_remember_reports_pending_not_a_bare_false(tmp_path, monkeypatch):
    """`notion_synced: false` was computed from a variable hardcoded to None
    on the line above, so every successful call reported it. Two sessions read
    that as a failed write and one logged it as a defect (SI-170) while the
    pages were in Notion the whole time. The response must distinguish
    "deferred" from "failed".
    """
    from recall.models import RememberResult
    from datetime import datetime, timezone

    r = RememberResult(si_id="SI-001", title="t",
                       created_at=datetime.now(timezone.utc),
                       tier="personal", message="m")
    assert r.notion_sync == "pending"
    assert r.notion_synced is False
    assert "notion_synced" not in r.model_dump()

    synced = r.model_copy(update={"notion_sync": "synced"})
    assert synced.notion_synced is True


class TestPullKeepsLocalIdentityAndFields:
    """A pull must not re-key a known page or reset what Notion cannot carry."""

    PAGE_ID = "aaaa-1111"

    def _engine(self):
        engine = MagicMock()
        engine.embed.return_value = b"\x00" * 16
        return engine

    def _db(self, tmp_path, si_id="PI-001", **overrides):
        from recall.db import RecallDB

        fields = dict(
            si_id=si_id,
            title="Piper TTS wrapper",
            symptoms="it broke",
            root_cause="bad config",
            fix="fix the config",
            source="~/Projects/clone, review",
            tags=["a,b", "python"],
            verified_at="2026-07",
            created_at=datetime(2026, 7, 1, 10, 20, 30, 123456, tzinfo=timezone.utc),
            tier="project",
            notion_page_id=self.PAGE_ID,
            notion_edited_at="2026-07-07T12:00:00.000Z",
        )
        fields.update(overrides)
        db = RecallDB(tmp_path / "t.db")
        db.insert_issue(Issue(**fields))
        return db

    def _pull(self, db, **page):
        client = MagicMock()
        client.query_all_pages.return_value = [_page(page_id=self.PAGE_ID, **page)]
        return sync_from_notion(db, self._engine(), client)

    @pytest.mark.parametrize("si_id", ["PI-001", "SI-105", "N-aaaa1111"])
    def test_title_without_id_keeps_local_id(self, tmp_path, si_id):
        db = self._db(tmp_path, si_id=si_id)
        assert self._pull(db, title="Piper TTS wrapper, edited") == 1
        assert db.count() == 1
        assert db.get_issue_by_id(si_id).title == "Piper TTS wrapper, edited"
        db.close()

    def test_title_with_different_id_still_rekeys_and_keeps_tier(self, tmp_path):
        db = self._db(tmp_path, si_id="N-aaaa1111")
        self._pull(db, title="SI-113 Piper TTS wrapper")
        assert db.count() == 1
        assert db.get_issue_by_id("SI-113").tier == "project"
        db.close()

    def test_leading_pi_id_in_title_is_used(self):
        assert derive_si_id("abc", "PI-002 — hashcat format") == "PI-002"

    def test_pi_mention_later_in_title_is_not_an_id(self):
        assert derive_si_id("abc-def", "Follow-up to PI-002") == "N-abcdef"

    def test_real_edit_keeps_local_only_fields(self, tmp_path):
        db = self._db(tmp_path)
        self._pull(db, solution="Symptoms: new\nRoot cause: bad config\nFix: fix the config")
        got = db.get_issue_by_id("PI-001")
        assert got.symptoms == "new"
        assert got.tier == "project"
        assert got.verified_at == "2026-07"
        assert got.created_at == datetime(2026, 7, 1, 10, 20, 30, 123456, tzinfo=timezone.utc)
        db.close()

    def test_notion_form_of_local_values_is_not_an_edit(self, tmp_path):
        """Notion strips commas and the push prefixes the ID onto the title."""
        symptoms = "log said 'Fix: rerun' then hung"
        db = self._db(tmp_path, symptoms=symptoms)
        self._pull(
            db,
            title="PI-001 — Piper TTS wrapper",
            solution=f"Symptoms: {symptoms}\nRoot cause: bad config\nFix: fix the config",
            project="~/Projects/clone  review",
            tags=["a", "b", "python"],
            severity="Medium",
        )
        got = db.get_issue_by_id("PI-001")
        assert got.title == "Piper TTS wrapper"
        assert got.symptoms == symptoms
        assert got.root_cause == "bad config"
        assert got.fix == "fix the config"
        assert got.source == "~/Projects/clone, review"
        assert got.tags == ["a", "b", "python"]  # local db splits tags on commas
        db.close()

    def test_option_casing_from_notion_is_not_an_edit(self, tmp_path):
        """Notion returns an existing option's casing, not the pushed one."""
        db = self._db(tmp_path, source="FusionAL-recall", tags=["moa", "Python"])
        self._pull(db, project="FusionAL-Recall", tags=["MoA", "python"], severity="Medium")
        got = db.get_issue_by_id("PI-001")
        assert got.source == "FusionAL-recall"
        assert got.tags == ["moa", "Python"]
        db.close()

    @pytest.mark.parametrize("title", ["Piper TTS wrapper", "Piper TTS wrapper, renamed"])
    def test_provenance_footer_is_not_pulled_into_the_row(self, tmp_path, title):
        """Some pages keep Source and ID in a Solution footer, Project=General."""
        db = self._db(tmp_path)
        self._pull(
            db,
            title=title,
            solution=(
                "Symptoms: it broke\nRoot cause: bad config\nFix: fix the config"
                "\n\nSource: ~/Projects/clone, review\nRecall ID: PI-001"
            ),
            project="General",
        )
        got = db.get_issue_by_id("PI-001")
        assert got.title == title
        assert got.fix == "fix the config"
        assert got.source == "~/Projects/clone, review"
        db.close()

    def test_changed_source_and_tags_are_applied(self, tmp_path):
        db = self._db(tmp_path)
        self._pull(db, project="Other", tags=["new-tag"], severity=None)
        got = db.get_issue_by_id("PI-001")
        assert got.source == "Other"
        assert got.tags == ["new-tag"]
        db.close()

    def test_missing_edit_time_is_adopted_on_unchanged_pushed_row(self, tmp_path):
        """Rows linked by older code have no stamp and get one on the next pull."""
        db = self._db(tmp_path, notion_edited_at=None)
        self._pull(db, title="PI-001 — Piper TTS wrapper")
        got = db.get_issue_by_id("PI-001")
        assert got.tier == "project"
        assert got.notion_edited_at == "2026-07-07T12:30:00.000Z"
        db.close()

    def test_push_then_pull_leaves_row_untouched(self, tmp_path):
        from recall.notion_sync import sync_to_notion

        db = self._db(
            tmp_path, si_id="SI-400", notion_page_id=None,
            notion_edited_at=None, notion_sync_pending=True,
        )
        before = db.get_issue_by_id("SI-400")
        client = MagicMock()
        client.create_page.return_value = (self.PAGE_ID, "2026-07-07T12:30:00.000Z")
        assert sync_to_notion(db, client) == 1
        client.query_all_pages.return_value = [
            _page(page_id=self.PAGE_ID, title="SI-400 — Piper TTS wrapper")
        ]
        assert sync_from_notion(db, self._engine(), client) == 0
        after = db.get_issue_by_id("SI-400")
        for name in ("title", "source", "tags", "tier", "created_at", "verified_at"):
            assert getattr(after, name) == getattr(before, name)
        db.close()
