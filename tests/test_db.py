import struct
import pytest
from recall.db import RecallDB
from recall.models import Issue


def _make_issue(si_id="SI-001", title="Test issue", tier="personal"):
    embedding = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
    return Issue(
        si_id=si_id,
        title=title,
        symptoms="Something broke",
        root_cause="Wrong config",
        fix="Fix the config",
        source="test session",
        tags=["test", "config"],
        verified_at="2026-01",
        tier=tier,
        embedding=embedding,
    )


def test_insert_and_get(db):
    issue = _make_issue()
    db.insert_issue(issue)
    result = db.get_issue_by_id("SI-001")
    assert result is not None
    assert result.title == "Test issue"
    assert result.tags == ["test", "config"]


def test_count_empty(db):
    assert db.count() == 0


def test_count_after_insert(db):
    db.insert_issue(_make_issue("SI-001"))
    db.insert_issue(_make_issue("SI-002"))
    assert db.count() == 2


def test_get_next_si_id_empty(db):
    assert db.next_si_id() == "SI-001"


def test_get_next_si_id_after_inserts(db):
    db.insert_issue(_make_issue("SI-001"))
    db.insert_issue(_make_issue("SI-005"))
    assert db.next_si_id() == "SI-006"


def test_list_recent(db):
    db.insert_issue(_make_issue("SI-001", "First"))
    db.insert_issue(_make_issue("SI-002", "Second"))
    issues = db.list_recent_issues(n=10)
    assert len(issues) == 2


def test_list_recent_tier_filter(db):
    db.insert_issue(_make_issue("SI-001", tier="personal"))
    db.insert_issue(_make_issue("SI-002", tier="public"))
    personal = db.list_recent_issues(tier="personal")
    assert len(personal) == 1
    assert personal[0].si_id == "SI-001"


def test_search_by_embedding_returns_sorted(db):
    e1 = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
    e2 = struct.pack("4f", 0.0, 1.0, 0.0, 0.0)
    i1 = _make_issue("SI-001", "Exact match")
    i1.embedding = e1
    i2 = _make_issue("SI-002", "Orthogonal")
    i2.embedding = e2
    db.insert_issue(i1)
    db.insert_issue(i2)
    results = db.search_by_embedding(e1, limit=5)
    assert results[0].si_id == "SI-001"
    assert results[0].similarity > results[1].similarity


def test_get_missing_returns_none(db):
    assert db.get_issue_by_id("SI-999") is None


class TestNotionColumns:
    def test_migration_adds_columns_to_existing_db(self, tmp_path):
        """Opening a pre-migration DB adds notion columns without data loss."""
        import sqlite3

        db_file = tmp_path / "old.db"
        conn = sqlite3.connect(db_file)
        conn.executescript(
            """
            CREATE TABLE issues (
                si_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                symptoms TEXT NOT NULL DEFAULT '', root_cause TEXT NOT NULL DEFAULT '',
                fix TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '', verified_at TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                tier TEXT NOT NULL DEFAULT 'personal', embedding BLOB
            );
            INSERT INTO issues (si_id, title) VALUES ('SI-001', 'legacy row');
            """
        )
        conn.commit()
        conn.close()

        from recall.db import RecallDB

        db = RecallDB(db_file)
        issue = db.get_issue_by_id("SI-001")
        assert issue is not None and issue.title == "legacy row"
        assert issue.notion_page_id is None
        db.close()

    def test_notion_fields_roundtrip(self, tmp_path):
        from datetime import datetime, timezone

        from recall.db import RecallDB
        from recall.models import Issue

        db = RecallDB(tmp_path / "t.db")
        db.insert_issue(
            Issue(
                si_id="N-aaaa1111", title="synced", symptoms="s", root_cause="r",
                fix="f", created_at=datetime.now(timezone.utc),
                notion_page_id="aaaa1111-0000", notion_edited_at="2026-07-07T12:30:00.000Z",
            )
        )
        got = db.get_issue_by_id("N-aaaa1111")
        assert got.notion_page_id == "aaaa1111-0000"
        assert got.notion_edited_at == "2026-07-07T12:30:00.000Z"
        db.close()

    def test_notion_sync_state(self, tmp_path):
        from datetime import datetime, timezone

        from recall.db import RecallDB
        from recall.models import Issue

        db = RecallDB(tmp_path / "t.db")
        db.insert_issue(Issue(si_id="SI-001", title="local only", symptoms="", root_cause="", fix="", created_at=datetime.now(timezone.utc)))
        db.insert_issue(Issue(si_id="N-bbbb2222", title="synced", symptoms="", root_cause="", fix="", created_at=datetime.now(timezone.utc), notion_page_id="bbbb2222-0000", notion_edited_at="2026-07-07T00:00:00.000Z"))
        state = db.notion_sync_state()
        assert state == {"bbbb2222-0000": "2026-07-07T00:00:00.000Z"}
        db.close()

    def test_upsert_same_si_id_replaces(self, tmp_path):
        from datetime import datetime, timezone

        from recall.db import RecallDB
        from recall.models import Issue

        db = RecallDB(tmp_path / "t.db")
        for edited in ("2026-07-07T00:00:00.000Z", "2026-07-08T00:00:00.000Z"):
            db.insert_issue(Issue(si_id="N-cccc3333", title=f"v-{edited}", symptoms="", root_cause="", fix="", created_at=datetime.now(timezone.utc), notion_page_id="cccc3333-0000", notion_edited_at=edited))
        assert db.count() == 1
        assert db.get_issue_by_id("N-cccc3333").notion_edited_at == "2026-07-08T00:00:00.000Z"
        db.close()
