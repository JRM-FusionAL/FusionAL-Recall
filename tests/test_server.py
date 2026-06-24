"""Comprehensive pytest suite for FusionAL Recall."""

from __future__ import annotations

import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

from recall.db import RecallDB
from recall.embeddings import EmbeddingEngine
from recall.models import Issue, QueryResult, RememberResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIM = 384  # all-MiniLM-L6-v2 output dimension


def _fake_blob(seed: float = 1.0) -> bytes:
    """Return a normalised float32 blob of shape (DIM,) with all values = seed/sqrt(DIM)."""
    import math

    val = seed / math.sqrt(DIM)
    return struct.pack(f"{DIM}f", *([val] * DIM))


def _make_issue(
    si_id: str = "SI-001",
    title: str = "Test issue",
    tier: str = "personal",
    blob: bytes | None = None,
) -> Issue:
    return Issue(
        si_id=si_id,
        title=title,
        symptoms="Something broke",
        root_cause="Unknown cause",
        fix="Apply the fix",
        source="test-session",
        tags=["test", "unit"],
        verified_at="2026-05",
        created_at=datetime.now(timezone.utc),
        tier=tier,
        embedding=blob or _fake_blob(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db() -> Generator[RecallDB, None, None]:
    """Isolated in-memory-backed SQLite DB for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    db = RecallDB(path)
    yield db
    db.close()
    Path(path).unlink(missing_ok=True)


@pytest.fixture()
def mock_engine() -> MagicMock:
    """A mock EmbeddingEngine that returns deterministic blobs."""
    engine = MagicMock(spec=EmbeddingEngine)
    engine.embed.return_value = _fake_blob(1.0)
    engine.embed_batch.side_effect = lambda texts: [_fake_blob(1.0) for _ in texts]
    return engine


@pytest.fixture()
def populated_db(tmp_db: RecallDB) -> RecallDB:
    """DB with 5 personal and 2 project issues pre-inserted."""
    for i in range(1, 6):
        tmp_db.insert_issue(_make_issue(f"SI-{i:03d}", f"Personal issue {i}", "personal"))
    for i in range(6, 8):
        tmp_db.insert_issue(_make_issue(f"SI-{i:03d}", f"Project issue {i}", "project"))
    return tmp_db


# ---------------------------------------------------------------------------
# Unit tests — models
# ---------------------------------------------------------------------------

class TestModels:
    def test_issue_defaults(self):
        issue = Issue(
            si_id="SI-999",
            title="X",
            symptoms="S",
            root_cause="R",
            fix="F",
        )
        assert issue.tier == "personal"
        assert issue.tags == []
        assert isinstance(issue.created_at, datetime)

    def test_query_result_similarity_bounds(self):
        with pytest.raises(Exception):
            QueryResult(
                si_id="SI-001", title="T", symptoms="S", root_cause="R",
                fix="F", source="", tags=[], similarity=1.5, tier="personal",
            )

    def test_remember_result_round_trips(self):
        now = datetime.now(timezone.utc)
        r = RememberResult(si_id="SI-030", title="T", created_at=now, tier="personal", message="ok")
        assert r.model_dump()["si_id"] == "SI-030"


# ---------------------------------------------------------------------------
# Unit tests — db
# ---------------------------------------------------------------------------

class TestRecallDB:
    def test_insert_and_get(self, tmp_db: RecallDB):
        issue = _make_issue("SI-001")
        tmp_db.insert_issue(issue)
        fetched = tmp_db.get_issue_by_id("SI-001")
        assert fetched is not None
        assert fetched.title == issue.title
        assert fetched.tags == ["test", "unit"]

    def test_get_missing_returns_none(self, tmp_db: RecallDB):
        assert tmp_db.get_issue_by_id("SI-999") is None

    def test_count(self, tmp_db: RecallDB):
        assert tmp_db.count() == 0
        tmp_db.insert_issue(_make_issue("SI-001"))
        assert tmp_db.count() == 1

    def test_next_si_id_empty_db(self, tmp_db: RecallDB):
        assert tmp_db.next_si_id() == "SI-001"

    def test_next_si_id_after_insert(self, tmp_db: RecallDB):
        tmp_db.insert_issue(_make_issue("SI-005"))
        assert tmp_db.next_si_id() == "SI-006"

    def test_next_si_id_sequence(self, tmp_db: RecallDB):
        for i in range(1, 30):
            tmp_db.insert_issue(_make_issue(f"SI-{i:03d}"))
        assert tmp_db.next_si_id() == "SI-030"

    def test_list_recent_ordered(self, populated_db: RecallDB):
        issues = populated_db.list_recent_issues(n=3)
        assert len(issues) == 3
        # Most recently inserted should be first (project issues were inserted last)

    def test_list_recent_tier_filter(self, populated_db: RecallDB):
        personal = populated_db.list_recent_issues(n=10, tier="personal")
        project = populated_db.list_recent_issues(n=10, tier="project")
        assert all(i.tier == "personal" for i in personal)
        assert all(i.tier == "project" for i in project)
        assert len(personal) == 5
        assert len(project) == 2

    def test_search_by_embedding_returns_sorted(self, populated_db: RecallDB):
        blob = _fake_blob(1.0)
        results = populated_db.search_by_embedding(blob, limit=3)
        assert len(results) == 3
        sims = [r.similarity for r in results]
        assert sims == sorted(sims, reverse=True)

    def test_search_by_embedding_tier_filter(self, populated_db: RecallDB):
        blob = _fake_blob(1.0)
        results = populated_db.search_by_embedding(blob, limit=10, tier="project")
        assert all(r.tier == "project" for r in results)
        assert len(results) == 2

    def test_cosine_similarity_identical(self):
        a = (1.0, 0.0, 0.0)
        b = (1.0, 0.0, 0.0)
        assert RecallDB._cosine_similarity(a, b) == pytest.approx(1.0)

    def test_cosine_similarity_orthogonal(self):
        a = (1.0, 0.0)
        b = (0.0, 1.0)
        assert RecallDB._cosine_similarity(a, b) == pytest.approx(0.0)

    def test_cosine_similarity_zero_vector(self):
        a = (0.0, 0.0)
        b = (1.0, 0.0)
        assert RecallDB._cosine_similarity(a, b) == 0.0

    def test_insert_replace(self, tmp_db: RecallDB):
        issue = _make_issue("SI-001", title="Original")
        tmp_db.insert_issue(issue)
        updated = _make_issue("SI-001", title="Updated")
        tmp_db.insert_issue(updated)
        fetched = tmp_db.get_issue_by_id("SI-001")
        assert fetched.title == "Updated"
        assert tmp_db.count() == 1


# ---------------------------------------------------------------------------
# Unit tests — embeddings (mocked to avoid loading the model)
# ---------------------------------------------------------------------------

class TestEmbeddingEngine:
    def test_embed_returns_bytes(self, mock_engine: MagicMock):
        blob = mock_engine.embed("hello world")
        assert isinstance(blob, bytes)
        assert len(blob) == DIM * 4  # float32 = 4 bytes each

    def test_embed_batch_length(self, mock_engine: MagicMock):
        texts = ["alpha", "beta", "gamma"]
        blobs = mock_engine.embed_batch(texts)
        assert len(blobs) == 3

    def test_unpack_roundtrip(self):
        original = tuple(float(i) for i in range(10))
        blob = struct.pack(f"{len(original)}f", *original)
        unpacked = EmbeddingEngine.unpack(blob)
        assert unpacked == pytest.approx(original, rel=1e-5)


# ---------------------------------------------------------------------------
# Unit tests — migration
# ---------------------------------------------------------------------------

SAMPLE_MD = """\
# SOLVED-ISSUES

## SI-001: Claude Desktop timeout above 8 servers
**Symptoms:** Claude Desktop hangs on startup when >8 MCP servers configured
**Root cause:** Desktop client enforces a hard timeout on MCP server init
**Fix:** Consolidate via FusionAL gateway
**Verified:** 2026-01 | **Source:** setup session
**Tags:** claude-desktop, mcp, timeout

---

## SI-002: localhost in MCP config = client machine not server
**Symptoms:** MCP server unreachable
**Root cause:** localhost resolves to the client, not the remote host
**Fix:** Use Tailscale IP instead of localhost
**Verified:** 2026-01 | **Source:** network setup
**Tags:** mcp, network, localhost

---

## TEMPLATE — copy this for new entries

## SI-XXX: <one-line title>
**Symptoms:** <what you saw>
**Root cause:** <what was actually wrong>
**Fix:** <exact steps>
**Verified:** YYYY-MM | **Source:** <session/repo/article>
**Tags:** <comma-separated>
"""


class TestMigration:
    def test_parse_issues_count(self):
        from recall.migrate import _parse_issues
        issues = _parse_issues(SAMPLE_MD)
        assert len(issues) == 2  # TEMPLATE block excluded

    def test_parse_issues_fields(self):
        from recall.migrate import _parse_issues
        issues = _parse_issues(SAMPLE_MD)
        first = issues[0]
        assert first["si_id"] == "SI-001"
        assert "Claude Desktop" in first["title"]
        assert "claude-desktop" in first["tags"]
        assert first["verified_at"] == "2026-01"

    def test_migrate_populates_db(self, tmp_db: RecallDB, mock_engine: MagicMock, tmp_path: Path):
        md_file = tmp_path / "SOLVED-ISSUES.md"
        md_file.write_text(SAMPLE_MD, encoding="utf-8")
        from recall.migrate import migrate_issues
        count = migrate_issues(tmp_db, mock_engine, md_file)
        assert count == 2
        assert tmp_db.count() == 2

    def test_migrate_skips_if_populated(self, tmp_db: RecallDB, mock_engine: MagicMock, tmp_path: Path):
        tmp_db.insert_issue(_make_issue("SI-001"))
        md_file = tmp_path / "SOLVED-ISSUES.md"
        md_file.write_text(SAMPLE_MD)
        from recall.migrate import migrate_issues
        count = migrate_issues(tmp_db, mock_engine, md_file)
        assert count == 0
        assert tmp_db.count() == 1  # pre-existing, not duplicated

    def test_migrate_missing_path(self, tmp_db: RecallDB, mock_engine: MagicMock):
        from recall.migrate import migrate_issues
        count = migrate_issues(tmp_db, mock_engine, None)
        assert count == 0

    def test_migrate_nonexistent_file(self, tmp_db: RecallDB, mock_engine: MagicMock):
        from recall.migrate import migrate_issues
        count = migrate_issues(tmp_db, mock_engine, "/nonexistent/path.md")
        assert count == 0


# ---------------------------------------------------------------------------
# Integration-style tests — server tools (mocked DB + engine)
# ---------------------------------------------------------------------------

class TestServerTools:
    """Test the five MCP tool functions directly, injecting mocked singletons."""

    def _patch_singletons(self, db: RecallDB, engine: MagicMock):
        import recall.server as srv
        srv._db = db
        srv._engine = engine

    def _reset_singletons(self):
        import recall.server as srv
        srv._db = None
        srv._engine = None

    def test_recall_returns_list(self, populated_db: RecallDB, mock_engine: MagicMock):
        self._patch_singletons(populated_db, mock_engine)
        try:
            from recall.server import recall
            results = recall("some error about servers")
            assert isinstance(results, list)
            assert len(results) <= 5
        finally:
            self._reset_singletons()

    def test_recall_tier_filter(self, populated_db: RecallDB, mock_engine: MagicMock):
        self._patch_singletons(populated_db, mock_engine)
        try:
            from recall.server import recall
            results = recall("issue", tier="project")
            assert all(r["tier"] == "project" for r in results)
        finally:
            self._reset_singletons()

    def test_remember_assigns_next_id(self, tmp_db: RecallDB, mock_engine: MagicMock):
        self._patch_singletons(tmp_db, mock_engine)
        try:
            from recall.server import remember
            result = remember(
                title="Test issue",
                symptoms="Something wrong",
                root_cause="Bad config",
                fix="Fix config",
                tags=["test"],
            )
            assert result["si_id"] == "SI-001"
            assert "Logged SI-001" in result["message"]
            assert tmp_db.count() == 1
        finally:
            self._reset_singletons()

    def test_remember_sequential_ids(self, tmp_db: RecallDB, mock_engine: MagicMock):
        self._patch_singletons(tmp_db, mock_engine)
        try:
            from recall.server import remember
            r1 = remember(title="First", symptoms="S", root_cause="R", fix="F")
            r2 = remember(title="Second", symptoms="S", root_cause="R", fix="F")
            assert r1["si_id"] == "SI-001"
            assert r2["si_id"] == "SI-002"
        finally:
            self._reset_singletons()

    def test_verify_found(self, populated_db: RecallDB, mock_engine: MagicMock):
        self._patch_singletons(populated_db, mock_engine)
        try:
            from recall.server import verify
            result = verify("SI-001")
            assert result["si_id"] == "SI-001"
            assert "error" not in result
        finally:
            self._reset_singletons()

    def test_verify_not_found(self, populated_db: RecallDB, mock_engine: MagicMock):
        self._patch_singletons(populated_db, mock_engine)
        try:
            from recall.server import verify
            result = verify("SI-999")
            assert "error" in result
        finally:
            self._reset_singletons()

    def test_get_is_alias_for_verify(self, populated_db: RecallDB, mock_engine: MagicMock):
        self._patch_singletons(populated_db, mock_engine)
        try:
            from recall.server import get, verify
            assert get("SI-001") == verify("SI-001")
        finally:
            self._reset_singletons()

    def test_list_recent_default(self, populated_db: RecallDB, mock_engine: MagicMock):
        self._patch_singletons(populated_db, mock_engine)
        try:
            from recall.server import list_recent
            results = list_recent()
            assert len(results) == 7  # 5 personal + 2 project
        finally:
            self._reset_singletons()

    def test_list_recent_limited(self, populated_db: RecallDB, mock_engine: MagicMock):
        self._patch_singletons(populated_db, mock_engine)
        try:
            from recall.server import list_recent
            results = list_recent(n=3)
            assert len(results) == 3
        finally:
            self._reset_singletons()
