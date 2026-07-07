# Notion Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recall indexes the Notion Solved Issues DB directly; `remember` dual-writes to Notion and SQLite.

**Architecture:** New `recall/notion_sync.py` holds a thin httpx Notion client, page→Issue mapping, and an incremental `sync_from_notion` upsert keyed on Notion page ID + `last_edited_time`. `recall/db.py` gains two nullable columns. `recall/server.py` wires sync into startup, a background thread, and dual-write `remember`. Spec: `docs/superpowers/specs/2026-07-07-notion-sync-design.md`.

**Tech Stack:** Python 3.12, FastMCP, httpx (already installed via mcp), SQLite, fastembed, pytest.

## Global Constraints

- Venv: `~/Projects/FusionAL-Recall/venv` (Python 3.12). Run tests with `venv/bin/pytest`. Do NOT recreate the venv.
- No new runtime dependencies — httpx is already present.
- Notion data source ID default: `836a9fe2-738d-4fdf-90a5-4364e1b36f1f`.
- Notion schema: `Issue` (title), `Solution` (rich_text), `Project` (select), `Tags` (multi_select), `Severity` (select: Critical|High|Medium|Low).
- Sync must NEVER raise to the server on Notion/network failure — log a warning, return 0.
- Embedded text for synced entries: `f"{issue.title} {issue.symptoms} {issue.root_cause}"` (matches `remember`).
- Work on branch `feat/notion-sync` off `main`.
- Notion rich_text objects cap at 2000 chars per text block — chunk long strings.

---

### Task 0: Branch + test deps

**Files:** none (setup only)

- [ ] **Step 1: Create branch**

```bash
cd ~/Projects/FusionAL-Recall && git checkout -b feat/notion-sync
```

- [ ] **Step 2: Ensure test deps present** (venv was rebuilt 2026-07-07 without dev extras)

```bash
venv/bin/pip install --no-cache-dir pytest pytest-asyncio pytest-cov
```

- [ ] **Step 3: Verify existing suite is green before touching anything**

Run: `venv/bin/pytest tests/ -q`
Expected: all pass (suite mocks the embedding model; no downloads). If anything fails, STOP and report — do not build on a red baseline.

---

### Task 1: Solution parsing + page→Issue mapping

**Files:**
- Create: `recall/notion_sync.py`
- Modify: `recall/models.py` (add 2 optional fields to `Issue`)
- Test: `tests/test_notion_sync.py`

**Interfaces:**
- Consumes: `recall.models.Issue` (existing pydantic model).
- Produces: `parse_solution(text: str) -> dict[str, str]` (keys `symptoms`, `root_cause`, `fix`); `map_page_to_issue(page: dict) -> Issue | None`; `derive_si_id(page_id: str, title: str, solution: str) -> str`; `Issue.notion_page_id: str | None`, `Issue.notion_edited_at: str | None`.

> **Learn by Doing checkpoint (inline execution only):** `parse_solution` encodes the policy for splitting Notion's combined Solution text. Stub it with `TODO(human)` and ask the human to implement it before proceeding. If executing via subagents, skip the checkpoint and use the reference implementation in Step 3.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notion_sync.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_notion_sync.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'recall.notion_sync'`

- [ ] **Step 3: Implement**

Add to `recall/models.py`, inside `class Issue`, after the `embedding` field:

```python
    notion_page_id: Optional[str] = Field(None, description="Linked Notion page ID")
    notion_edited_at: Optional[str] = Field(None, description="Notion last_edited_time at last sync")
```

Create `recall/notion_sync.py`:

```python
"""Sync the Notion Solved Issues database into the local recall index.

Notion is the canonical registry; recall.db is a derived semantic index.
See docs/superpowers/specs/2026-07-07-notion-sync-design.md.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import RecallDB
    from .embeddings import EmbeddingEngine

from .models import Issue

log = logging.getLogger(__name__)

_SI_ID = re.compile(r"\bSI-\d{3}\b")
_LABEL_PATTERNS = (
    ("symptoms", r"symptoms"),
    ("root_cause", r"root\s+cause"),
    ("fix", r"fix"),
)


def parse_solution(text: str) -> dict[str, str]:
    """Split a combined Solution text into symptoms / root_cause / fix.

    The logging skill writes 'Symptoms: ...', 'Root cause: ...', 'Fix: ...'
    but entries vary: labels may be newline- or sentence-separated and any
    case. Unlabeled text lands whole in 'fix'.
    """
    parts = {"symptoms": "", "root_cause": "", "fix": ""}
    found: list[tuple[int, int, str]] = []
    for key, pat in _LABEL_PATTERNS:
        m = re.search(rf"(?i)\b{pat}\s*:\s*", text)
        if m:
            found.append((m.start(), m.end(), key))
    if not found:
        parts["fix"] = text.strip()
        return parts
    found.sort()
    for i, (_, end, key) in enumerate(found):
        nxt = found[i + 1][0] if i + 1 < len(found) else len(text)
        parts[key] = text[end:nxt].strip()
    return parts


def derive_si_id(page_id: str, title: str, solution: str) -> str:
    """Reuse a legacy SI-ID if the entry references one; else derive from page ID."""
    m = _SI_ID.search(title) or _SI_ID.search(solution)
    if m:
        return m.group(0)
    return "N-" + page_id.replace("-", "")[:8]


def _plain_text(rich: list[dict]) -> str:
    return "".join(r.get("plain_text", "") for r in rich)


def _select_name(prop: dict) -> str:
    return (prop.get("select") or {}).get("name", "") if prop else ""


def map_page_to_issue(page: dict) -> Issue | None:
    """Map a Notion page object to an Issue. Returns None for pages with no title."""
    props = page.get("properties", {})
    title = _plain_text(props.get("Issue", {}).get("title", []))
    if not title.strip():
        return None
    solution = _plain_text(props.get("Solution", {}).get("rich_text", []))
    parsed = parse_solution(solution)
    tags = [t["name"] for t in props.get("Tags", {}).get("multi_select", []) or []]
    severity = _select_name(props.get("Severity", {}))
    if severity:
        tags.append(f"severity:{severity}")
    created_raw = page.get("created_time", "")
    try:
        created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    except ValueError:
        created_at = datetime.now(timezone.utc)
    return Issue(
        si_id=derive_si_id(page["id"], title, solution),
        title=title.strip(),
        symptoms=parsed["symptoms"],
        root_cause=parsed["root_cause"],
        fix=parsed["fix"],
        source=_select_name(props.get("Project", {})),
        tags=tags,
        created_at=created_at,
        tier="personal",
        embedding=None,
        notion_page_id=page["id"],
        notion_edited_at=page.get("last_edited_time"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest tests/test_notion_sync.py -q`
Expected: all PASS. Also run the full suite: `venv/bin/pytest tests/ -q` — the `Issue` model change must not break existing tests.

- [ ] **Step 5: Commit**

```bash
git add recall/notion_sync.py recall/models.py tests/test_notion_sync.py
git commit -m "feat: parse Notion Solved Issues pages into Issue records"
```

---

### Task 2: DB schema migration + sync state

**Files:**
- Modify: `recall/db.py` (`_init_schema`, `insert_issue`, `_row_to_issue`; new method `notion_sync_state`)
- Test: `tests/test_db.py` (append new test class)

**Interfaces:**
- Consumes: `Issue.notion_page_id`, `Issue.notion_edited_at` from Task 1.
- Produces: `RecallDB.notion_sync_state() -> dict[str, str]` mapping `notion_page_id -> notion_edited_at` (empty string when NULL). `insert_issue` persists the two new fields; `INSERT OR REPLACE` on `si_id` remains the upsert mechanism.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_db.py -q -k Notion`
Expected: FAIL — `Issue` has the fields (Task 1) but `insert_issue` doesn't persist them / `notion_sync_state` doesn't exist.

- [ ] **Step 3: Implement in `recall/db.py`**

At the end of `_init_schema` (after the existing `executescript`, before `commit`):

```python
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(issues)")}
        if "notion_page_id" not in cols:
            self._conn.execute("ALTER TABLE issues ADD COLUMN notion_page_id TEXT")
        if "notion_edited_at" not in cols:
            self._conn.execute("ALTER TABLE issues ADD COLUMN notion_edited_at TEXT")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notion_page ON issues(notion_page_id)"
        )
```

(SQLite can't `ADD COLUMN ... UNIQUE`; the separate unique index allows multiple NULLs, which is what legacy rows need.)

Replace `insert_issue`'s SQL and tuple:

```python
    def insert_issue(self, issue: Issue) -> None:
        tags_str = ",".join(issue.tags)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO issues
                (si_id, title, symptoms, root_cause, fix, source,
                 tags, verified_at, created_at, tier, embedding,
                 notion_page_id, notion_edited_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                issue.si_id,
                issue.title,
                issue.symptoms,
                issue.root_cause,
                issue.fix,
                issue.source,
                tags_str,
                issue.verified_at,
                issue.created_at.isoformat(),
                issue.tier,
                issue.embedding,
                issue.notion_page_id,
                issue.notion_edited_at,
            ),
        )
        self._conn.commit()
```

Add after `count()`:

```python
    def notion_sync_state(self) -> dict[str, str]:
        """Map notion_page_id -> notion_edited_at for all Notion-linked rows."""
        rows = self._conn.execute(
            "SELECT notion_page_id, notion_edited_at FROM issues"
            " WHERE notion_page_id IS NOT NULL"
        ).fetchall()
        return {r["notion_page_id"]: r["notion_edited_at"] or "" for r in rows}
```

In `_row_to_issue`, add to the `Issue(...)` constructor call:

```python
            notion_page_id=row["notion_page_id"],
            notion_edited_at=row["notion_edited_at"],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest tests/ -q`
Expected: all PASS (full suite — the live `recall.db` on disk must also survive: the migration is additive only).

- [ ] **Step 5: Commit**

```bash
git add recall/db.py tests/test_db.py
git commit -m "feat: add notion_page_id/notion_edited_at columns and sync-state query"
```

---

### Task 3: NotionClient + sync_from_notion

**Files:**
- Modify: `recall/notion_sync.py` (append client + sync function + property builder)
- Test: `tests/test_notion_sync.py` (append)

**Interfaces:**
- Consumes: `RecallDB.notion_sync_state()`, `RecallDB.insert_issue()` (Task 2); `EmbeddingEngine.embed(text) -> bytes`; `map_page_to_issue` (Task 1).
- Produces: `NotionClient(token, data_source_id, timeout=10.0)` with `.query_all_pages() -> list[dict]` and `.create_page(properties: dict) -> str` (returns page ID); `NotionSyncError(Exception)`; `sync_from_notion(db, engine, client) -> int`; `build_notion_properties(title, solution, project, tags, severity="Medium") -> dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notion_sync.py`:

```python
from unittest.mock import MagicMock, patch

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
            import pytest

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
        page = _page(page_id="aaaa-1")
        client.query_all_pages.return_value = [page]
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_notion_sync.py -q`
Expected: FAIL — `ImportError: cannot import name 'NotionClient'`

- [ ] **Step 3: Implement — append to `recall/notion_sync.py`**

```python
class NotionSyncError(Exception):
    """Raised when the Notion API returns an error response."""


class NotionClient:
    """Minimal Notion REST client — query + create only, no SDK dependency."""

    BASE = "https://api.notion.com/v1"

    def __init__(self, token: str, data_source_id: str, timeout: float = 10.0) -> None:
        self._token = token
        self._ds_id = data_source_id
        self._timeout = timeout

    def _post(self, path: str, payload: dict, version: str) -> dict:
        import httpx

        resp = httpx.post(
            f"{self.BASE}{path}",
            json=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise NotionSyncError(f"POST {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _query_once(self, payload: dict) -> dict:
        # Data-source endpoint (2025 API); fall back to classic database query.
        try:
            return self._post(f"/data_sources/{self._ds_id}/query", payload, "2025-09-03")
        except NotionSyncError:
            return self._post(f"/databases/{self._ds_id}/query", payload, "2022-06-28")

    def query_all_pages(self) -> list[dict]:
        pages: list[dict] = []
        cursor: str | None = None
        while True:
            payload: dict = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._query_once(payload)
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")

    def create_page(self, properties: dict) -> str:
        payload: dict = {
            "parent": {"type": "data_source_id", "data_source_id": self._ds_id},
            "properties": properties,
        }
        try:
            data = self._post("/pages", payload, "2025-09-03")
        except NotionSyncError:
            payload["parent"] = {"database_id": self._ds_id}
            data = self._post("/pages", payload, "2022-06-28")
        return data["id"]


def _chunk_rich_text(content: str, limit: int = 2000) -> list[dict]:
    chunks = [content[i : i + limit] for i in range(0, len(content), limit)] or [""]
    return [{"text": {"content": c}} for c in chunks]


def build_notion_properties(
    title: str,
    solution: str,
    project: str,
    tags: list[str],
    severity: str = "Medium",
) -> dict:
    """Properties payload for creating a Solved Issues page."""
    return {
        "Issue": {"title": _chunk_rich_text(title)},
        "Solution": {"rich_text": _chunk_rich_text(solution)},
        "Project": {"select": {"name": project or "General"}},
        "Tags": {"multi_select": [{"name": t} for t in tags]},
        "Severity": {"select": {"name": severity}},
    }


def sync_from_notion(db: "RecallDB", engine: "EmbeddingEngine", client: NotionClient) -> int:
    """Incrementally upsert Notion pages into the local index.

    Returns the number of rows inserted/updated. Never raises on
    Notion/network failure — the existing index keeps serving.
    """
    try:
        pages = client.query_all_pages()
    except Exception as exc:
        log.warning("notion sync: query failed, keeping existing index: %s", exc)
        return 0

    state = db.notion_sync_state()
    changed = 0
    for page in pages:
        page_id = page.get("id", "")
        edited = page.get("last_edited_time") or ""
        if page_id and edited and state.get(page_id) == edited:
            continue
        issue = map_page_to_issue(page)
        if issue is None:
            log.warning("notion sync: skipping page %s (no title)", page_id)
            continue
        issue.embedding = engine.embed(f"{issue.title} {issue.symptoms} {issue.root_cause}")
        db.insert_issue(issue)
        changed += 1
    log.info("notion sync: %d page(s) upserted, %d total in index", changed, db.count())
    return changed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add recall/notion_sync.py tests/test_notion_sync.py
git commit -m "feat: Notion client and incremental sync_from_notion"
```

---

### Task 4: Dual-write `remember`

**Files:**
- Modify: `recall/server.py` (config block, new `get_notion()` singleton, `remember` tool)
- Modify: `recall/models.py` (`RememberResult` gains `notion_synced: bool`)
- Test: `tests/test_notion_sync.py` (append)

**Interfaces:**
- Consumes: `NotionClient.create_page`, `build_notion_properties` (Task 3).
- Produces: `get_notion() -> NotionClient | None` (None when `NOTION_TOKEN` unset); `remember(...)` response gains `"notion_synced": bool`; stored issue carries `notion_page_id` on success. Config globals in `recall.server`: `NOTION_TOKEN`, `NOTION_DATA_SOURCE_ID`, `NOTION_SYNC_INTERVAL`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notion_sync.py`:

```python
class TestRememberDualWrite:
    def _setup(self, tmp_path, monkeypatch):
        from recall import server

        db_path = tmp_path / "t.db"
        monkeypatch.setattr(server, "_db", None)
        monkeypatch.setattr(server, "_engine", None)
        monkeypatch.setattr(server, "_notion", None)
        monkeypatch.setattr(server, "DB_PATH", str(db_path))
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
```

Note: the existing suite calls FastMCP tools as plain functions — if `@app.tool()` wraps them so direct calls fail, follow the pattern already used in `tests/test_server.py` for invoking tools.

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_notion_sync.py -q -k DualWrite`
Expected: FAIL — `AttributeError: module 'recall.server' has no attribute '_notion'`

- [ ] **Step 3: Implement**

In `recall/models.py`, add to `RememberResult`:

```python
    notion_synced: bool = False
```

In `recall/server.py`, add to the Configuration block:

```python
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATA_SOURCE_ID = os.getenv(
    "NOTION_DATA_SOURCE_ID", "836a9fe2-738d-4fdf-90a5-4364e1b36f1f"
)
NOTION_SYNC_INTERVAL = int(os.getenv("NOTION_SYNC_INTERVAL", "3600"))
```

Add to the Singletons block:

```python
_notion: "NotionClient | None" = None


def get_notion():
    """NotionClient singleton, or None when NOTION_TOKEN is unset."""
    global _notion
    if _notion is None and NOTION_TOKEN:
        from .notion_sync import NotionClient
        _notion = NotionClient(NOTION_TOKEN, NOTION_DATA_SOURCE_ID)
    return _notion
```

Replace the body of `remember` (keep the docstring, add to it: "Writes to the canonical Notion registry when configured; always writes locally."):

```python
    from .models import Issue, RememberResult
    from .notion_sync import build_notion_properties

    db = get_db()
    engine = get_engine()

    si_id = db.next_si_id()
    blob = engine.embed(f"{title} {symptoms} {root_cause}")

    notion_page_id: str | None = None
    client = get_notion()
    if client is not None:
        solution = f"Symptoms: {symptoms}\nRoot cause: {root_cause}\nFix: {fix}"
        try:
            notion_page_id = client.create_page(
                build_notion_properties(title, solution, source, tags or [])
            )
        except Exception as exc:
            log.warning("remember: Notion write failed, saving locally only: %s", exc)

    issue = Issue(
        si_id=si_id,
        title=title,
        symptoms=symptoms,
        root_cause=root_cause,
        fix=fix,
        source=source,
        tags=tags or [],
        created_at=datetime.now(timezone.utc),
        tier=tier,
        embedding=blob,
        notion_page_id=notion_page_id,
    )
    db.insert_issue(issue)

    result = RememberResult(
        si_id=si_id,
        title=title,
        created_at=issue.created_at,
        tier=tier,
        message=f"Logged {si_id}: {title}",
        notion_synced=notion_page_id is not None,
    )
    return result.model_dump()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest tests/ -q`
Expected: all PASS (existing `remember` tests must still pass — `get_notion()` returns None when no token, preserving old behavior).

- [ ] **Step 5: Commit**

```bash
git add recall/server.py recall/models.py tests/test_notion_sync.py
git commit -m "feat: remember dual-writes to Notion when NOTION_TOKEN is set"
```

---

### Task 5: Startup sync + background thread + deploy + live verify

**Files:**
- Modify: `recall/server.py` (`_on_startup`, new `_start_sync_thread`, `main`)
- Modify: `~/Projects/FusionAL-Recall/.env` (NOT committed — verify it's gitignored)
- Test: `tests/test_notion_sync.py` (append)

**Interfaces:**
- Consumes: `sync_from_notion`, `get_notion` (Tasks 3–4).
- Produces: startup Notion sync; hourly daemon thread; deployed service with `NOTION_TOKEN`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notion_sync.py`:

```python
class TestStartupSync:
    def test_startup_runs_notion_sync_when_configured(self, tmp_path, monkeypatch):
        from recall import server

        monkeypatch.setattr(server, "_db", None)
        monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "t.db"))
        monkeypatch.setattr(server, "SOLVED_ISSUES_PATH", None)
        engine = MagicMock()
        monkeypatch.setattr(server, "get_engine", lambda: engine)
        client = MagicMock()
        client.query_all_pages.return_value = [_page(page_id="eeee-5")]
        monkeypatch.setattr(server, "get_notion", lambda: client)
        engine.embed.return_value = b"\x00" * 16
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_notion_sync.py -q -k Startup`
Expected: FAIL — startup inserts 0 (sync not wired), first test asserts 1.

- [ ] **Step 3: Implement in `recall/server.py`**

Replace `_on_startup` and add the thread starter:

```python
def _on_startup() -> None:
    """Seed from SOLVED-ISSUES.md if empty, then sync from Notion (canonical)."""
    from .migrate import migrate_issues
    db = get_db()
    engine = get_engine()
    inserted = migrate_issues(db, engine, SOLVED_ISSUES_PATH)
    if inserted:
        log.info("startup: migrated %d issues from %s", inserted, SOLVED_ISSUES_PATH)

    client = get_notion()
    if client is not None:
        from .notion_sync import sync_from_notion
        synced = sync_from_notion(db, engine, client)
        log.info("startup: notion sync upserted %d issue(s)", synced)
    else:
        log.info("startup: NOTION_TOKEN not set, notion sync disabled")
    log.info("startup: DB ready with %d issues", db.count())


def _start_sync_thread() -> None:
    """Hourly background re-sync from Notion (daemon thread; failures logged)."""
    client = get_notion()
    if client is None or NOTION_SYNC_INTERVAL <= 0:
        return
    import threading
    import time

    def _loop() -> None:
        from .notion_sync import sync_from_notion
        while True:
            time.sleep(NOTION_SYNC_INTERVAL)
            try:
                sync_from_notion(get_db(), get_engine(), client)
            except Exception as exc:  # never kill the thread
                log.warning("background notion sync failed: %s", exc)

    threading.Thread(target=_loop, daemon=True, name="notion-sync").start()
```

Update `main`:

```python
def main() -> None:  # pragma: no cover
    _on_startup()
    _start_sync_thread()
    app.run(transport="streamable-http")
```

- [ ] **Step 4: Run full suite**

Run: `venv/bin/pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 5: Commit code**

```bash
git add recall/server.py tests/test_notion_sync.py
git commit -m "feat: startup Notion sync + hourly background refresh"
```

- [ ] **Step 6: Deploy — token into .env (never into git)**

```bash
cd ~/Projects/FusionAL-Recall
grep -q '^\.env$' .gitignore || echo '.env' >> .gitignore
grep -h '^NOTION_TOKEN=' ~/.hermes/.env >> .env 2>/dev/null || grep -h '^NOTION_API_KEY=' ~/Projects/FusionAL/.env | sed 's/^NOTION_API_KEY=/NOTION_TOKEN=/' >> .env
chmod 600 .env
```

Then verify the token can read the DB before restarting anything:

```bash
TOKEN=$(grep '^NOTION_TOKEN=' .env | cut -d= -f2-)
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  "https://api.notion.com/v1/databases/836a9fe2-738d-4fdf-90a5-4364e1b36f1f/query" \
  -H "Authorization: Bearer $TOKEN" -H "Notion-Version: 2022-06-28" \
  -H "Content-Type: application/json" -d '{"page_size": 1}'
```

Expected: `200`. If `404`, the integration isn't shared with the Solved Issues DB (share it via Notion UI → database ••• menu → Connections) or the ID needs the data-source endpoint (try `/v1/data_sources/.../query` with `Notion-Version: 2025-09-03`). If `401`, try the other token. STOP and report if neither works.

- [ ] **Step 7: Restart service and verify live**

```bash
systemctl --user restart fusional-recall
sleep 20
journalctl --user -u fusional-recall -n 20 --no-pager | grep -E "notion sync|DB ready"
curl -s http://localhost:8107/health
```

Expected: log line `notion sync upserted N issue(s)` with N ≥ 60 (first sync pulls every page), health returns ok.

- [ ] **Step 8: End-to-end proof — search for a Notion-only entry**

Query via MCP client (use the probe pattern from `~/.claude/skills/mcp-probe`): call `recall` with query "pip corrupt cache IncompleteRead" — the 2026-07-07 entry "fusional-recall crash-looped 1400+ restarts" exists ONLY in Notion, so finding it proves the sync path end-to-end. Expected: that entry in the top results with `si_id` starting `N-`.

- [ ] **Step 9: Commit deploy notes + update CLAUDE.md**

Update `CLAUDE.md` MCP Tool Summary table: `remember` row becomes "Log new SI entry to Notion (canonical) + local index". Add `NOTION_TOKEN`/`NOTION_SYNC_INTERVAL` to a new Config section. Also fix the stale Windows paths in the Recall Protocol section (point to Notion Solved Issues DB instead of `C:\Users\puddi\...`).

```bash
git add CLAUDE.md
git commit -m "docs: recall now indexes Notion Solved Issues directly"
```

---

## After all tasks

Use superpowers:finishing-a-development-branch — run the full suite one final time, then merge/PR `feat/notion-sync` to `main`.
