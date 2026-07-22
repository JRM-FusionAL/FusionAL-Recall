# FusionAL Recall — Architecture

> **Version:** 0.1.0  
> **Service:** `fusional-recall` — MCP server on port 8107  
> **Purpose:** Semantic solved-issues registry with three-tier access control  

## Overview

FusionAL Recall is a semantic search MCP server that wraps the **FusionAL Solved-Issues Registry** with embedding-based retrieval. It lets agents query past solutions by natural-language description — "claude desktop timeout" returns the matching SI entry even if the query words don't match the title exactly.

The service is deployed as a systemd unit on port 8107, backed by SQLite + fastembed (ONNX Runtime) for CPU-friendly vector search.

## High-Level Data Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                         External Sources                          │
│                                                                   │
│  SOLVED-ISSUES.md (markdown)     Notion Database (canonical)      │
│         │                               │                         │
│         ▼                               ▼                         │
│  ┌──────────────┐           ┌─────────────────────┐              │
│  │  migrate.py   │           │   notion_sync.py    │              │
│  │  (first run)  │           │  (startup + hourly) │              │
│  └──────┬───────┘           └─────────┬───────────┘              │
│         │                             │                           │
│         └──────────┬──────────────────┘                           │
│                    ▼                                              │
│  ┌─────────────────────────────────────┐                         │
│  │        SQLite + sqlite-vec          │                         │
│  │  (issues table + float32 blobs)     │                         │
│  └────────────┬────────────────────────┘                         │
│               │                                                   │
│               ▼                                                   │
│  ┌─────────────────────────────────────┐                         │
│  │       FastMCP Server (8107)         │                         │
│  │  ┌─────────┐  ┌──────────┐         │                         │
│  │  │ rec ail() │  │remember()│         │                         │
│  │  │list_rec ent│  │verify() │         │                         │
│  │  │  get()   │  │ /health │         │                         │
│  │  └─────────┘  └──────────┘         │                         │
│  └─────────────────────────────────────┘                         │
└──────────────────────────────────────────────────────────────────┘
```

## Module Map

```
recall/
├── __init__.py        # Package metadata (version, author)
├── models.py          # Pydantic v2 data models
├── db.py              # SQLite persistence + cosine similarity search
├── embeddings.py      # fastembed (ONNX) embedding engine
├── migrate.py         # SOLVED-ISSUES.md markdown parser + migration
├── notion_sync.py     # Notion REST client + bi-directional sync
└── server.py          # FastMCP server + MCP tool definitions

tests/
├── conftest.py        # Shared fixtures
├── test_db.py         # DB layer tests
├── test_server.py     # Server tool + integration tests
├── test_migration.py  # Markdown parsing tests
└── test_notion_sync.py # Notion sync + client tests
```

## Layer Details

### 1. Models (`models.py`)

Three Pydantic v2 models form the data contract:

| Model | Purpose | Key Fields |
|---|---|---|
| `Issue` | Full solved-issue record | `si_id`, `title`, `symptoms`, `root_cause`, `fix`, `source`, `tags`, `tier`, `embedding`, `notion_page_id` |
| `QueryResult` | Search result with similarity score | Inherits Issue fields + `similarity: float (0-1)` |
| `RememberResult` | Confirmation after logging | `si_id`, `title`, `created_at`, `message`, `notion_synced` |

- `Issue.embedding` is excluded from JSON serialization (`exclude=True`).
- `Issue.tier` defaults to `"personal"`.
- `Issue.tags` is a `List[str]`; the DB layer serializes it as a comma-separated string.

### 2. Persistence (`db.py`)

`RecallDB` — a thin SQLite wrapper with no ORM.

**Schema** (auto-created):

```sql
CREATE TABLE issues (
    si_id       TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    symptoms    TEXT NOT NULL DEFAULT '',
    root_cause  TEXT NOT NULL DEFAULT '',
    fix         TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '',
    verified_at TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tier        TEXT NOT NULL DEFAULT 'personal',
    embedding   BLOB,

    -- Added by additive migration (since it's NOT NULL-safe SQLite):
    notion_page_id   TEXT,
    notion_edited_at TEXT
);
CREATE INDEX idx_tier ON issues(tier);
CREATE INDEX idx_created ON issues(created_at DESC);
CREATE UNIQUE INDEX idx_notion_page ON issues(notion_page_id);
```

**Key operations:**

- `search_by_embedding(blob, limit, tier)` — loads all rows with non-NULL embeddings, computes **cosine similarity** in Python (`_cosine_similarity`), sorts descending, returns top-N as `QueryResult` objects. This is brute-force — no ANN index — acceptable at the expected scale (hundreds to low thousands of entries).
- `insert_issue(issue)` — `INSERT OR REPLACE` keyed on `si_id`.
- `next_si_id()` — parses the highest existing `SI-XXX` identifier and increments.
- `notion_sync_state()` — returns `{notion_page_id: notion_edited_at}` for all Notion-linked rows, enabling incremental sync.

### 3. Embeddings (`embeddings.py`)

`EmbeddingEngine` wraps **fastembed** (`sentence-transformers/all-MiniLM-L6-v2`) running on ONNX Runtime.

- **Why fastembed over sentence-transformers:** Avoids the PyTorch/AVX2 dependency. The same model weights run via ONNX export, so existing DB embeddings remain valid across the switch.
- **Output:** 384-dimensional float32 vectors packed as raw bytes (`struct.pack(f"{384}f", *vec)`) for direct SQLite BLOB storage.
- **Methods:** `embed(text) -> bytes`, `embed_batch(texts) -> List[bytes]`, `unpack(blob) -> tuple`.

### 4. Migration (`migrate.py`)

On first startup, the server reads `SOLVED-ISSUES.md` and populates the database:

1. **Parse** — regex-based markdown parser extracts `## SI-XXX: Title` headers and labelled fields (`**Symptoms:**`, `**Root cause:**`, `**Fix:**`, `**Verified:**`, **Source:**`, **Tags:**`).
2. **Skip** — The `## SI-XXX` template block (containing literal "XXX") is excluded.
3. **Embed** — Batch-embeds all titles via `embed_batch()`.
4. **Insert** — Writes each `Issue` with its float32 embedding blob.
5. **Idempotent** — Migration only runs if `db.count() == 0`; subsequent restarts skip it.

### 5. Notion Sync (`notion_sync.py`)

Notion is the **canonical registry**; the SQLite index is a derived semantic cache.

**Direction:** Notion → SQLite (one-way sync). New issues logged via `remember()` also write to Notion (dual-write), but Notion edits always overwrite local data on sync.

- **Client:** `NotionClient` — minimal REST client (no SDK). Supports data-source API (2025-09-03) with fallback to legacy database query (2022-06-28).
- **Pagination:** Full cursor-based traversal (`query_all_pages()`).
- **Identity:** Pages without an `SI-XXX` in their title get `N-<full-hex-page-id>` as their `si_id`. Full hex (not prefix) is used because Notion page IDs are time-ordered — short prefixes collide across pages created near each other.
- **Incremental sync:** Compares `last_edited_time` per page; only re-embeds and upserts changed pages.
- **Resilience:** Notion API failures are logged but never crash the server. The existing index continues serving.

**Sync triggers:**
1. **Startup** (`_on_startup()`) — runs immediately after markdown migration.
2. **Background thread** (`_start_sync_thread()`) — daemon thread re-syncs at `NOTION_SYNC_INTERVAL` seconds (default: 3600 = 1 hour).

**Parsing:** `parse_solution()` splits combined "Symptoms: ... Root cause: ... Fix: ..." text using case-insensitive regex. Unlabeled text falls through to `fix`.

### 6. Server (`server.py`)

FastMCP v3 API server with lazy singleton initialization (enables test mocking).

**MCP Tools:**

| Tool | Signature | Description |
|---|---|---|
| `recall` | `(query, tier?, limit=5)` | Semantic search — embed query, cosine-sort, return top results |
| `remember` | `(title, symptoms, root_cause, fix, source?, tags?, tier?)` | Log new issue → dual-write Notion → return `RememberResult` |
| `list_recent` | `(n=10, tier?)` | List newest issues ordered by `created_at DESC` |
| `verify` | `(si_id)` | Fetch full issue by ID, or return `{"error": "..."}` |
| `get` | `(si_id)` | Alias for `verify` |

**HTTP Endpoint:**

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check — returns `{"status": "ok", "service": "fusional-recall", "port": 8107}` |

**Singleton lifecycle:**

```python
_db = None          # RecallDB
_engine = None      # EmbeddingEngine
_notion = None      # NotionClient (None if NOTION_TOKEN unset)
```

Each `get_*()` factory lazy-initializes on first call. Tests inject mocks by overwriting these module globals.

**Startup sequence (`main()`):**

```
1. _on_startup()
   ├── migrate_issues(db, engine, SOLVED_ISSUES_PATH)  # markdown → SQLite
   └── sync_from_notion(db, engine, client)             # Notion → SQLite
2. _start_sync_thread()                                  # hourly re-sync
3. app.run(transport="streamable-http")                  # serve MCP
```

## Deployment

### Docker

```dockerfile
FROM python:3.11-slim
# Installs requirements.txt, copies recall/ module
# Default: RECALL_HOST=0.0.0.0 RECALL_PORT=8107 RECALL_DB_PATH=/data/recall.db
CMD ["python", "-m", "recall.server"]
```

### CI

GitHub Actions workflow (`.github/workflows/ci.yml`):
- Python 3.12 on ubuntu-latest
- Installs dependencies from `pyproject.toml` + dev extras
- Runs `pytest -v`

## Configuration

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `RECALL_HOST` | `0.0.0.0` | MCP server bind address |
| `RECALL_PORT` | `8107` | MCP server port |
| `RECALL_DB_PATH` | `./recall.db` | SQLite database file path |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model name |
| `DEFAULT_TIER` | `personal` | Default access tier for new entries |
| `SOLVED_ISSUES_PATH` | _(none)_ | Path to SOLVED-ISSUES.md (migration source) |
| `NOTION_TOKEN` | _(none)_ | Notion integration token (sync disabled when unset) |
| `NOTION_DATA_SOURCE_ID` | `836a9fe2-...` | Notion database / data-source ID |
| `NOTION_SYNC_INTERVAL` | `3600` | Background sync interval in seconds |

## Access Tiers

Three-tier access control enforced at search time:

| Tier | Scope | Intended Use |
|---|---|---|
| `personal` | Agent's own history | Per-agent solved issues |
| `project` | Team-scoped | Shared team solutions |
| `public` | Open | Community-contributed fixes |

Tier filtering happens in `search_by_embedding()` — the WHERE clause filters by `tier=` before cosine scoring.

## Testing Strategy

- **conftest.py** — in-memory `RecallDB` fixture; no disk I/O in unit tests.
- **test_db.py** — CRUD, SI-ID generation, tier filtering, cosine similarity math, Notion column additive migration.
- **test_server.py** — MCP tool functions via singleton injection (mocked DB + engine). Covers all five tools + edge cases.
- **test_migration.py** — markdown parsing with real and synthetic SOLVED-ISSUES.md content.
- **test_notion_sync.py** — `NotionClient` with HTTP mocks (httpx), pagination, fallback logic, solution parsing, page mapping, incremental sync, dual-write in `remember()`.

## Design Decisions

1. **Brute-force cosine search** — No ANN index. At the expected scale (<10K entries), scanning all embeddings in Python is fast enough and avoids index maintenance complexity.

2. **Notion as canonical source** — The Notion database is the system of record for solved issues. `recall.db` is a derived semantic index that can be rebuilt at any time.

3. **Dual-write on remember()** — New issues logged via MCP write to both Notion and SQLite. A Notion outage never blocks local logging (failure degrades to local-only with `notion_synced=False`).

4. **Lazy singletons** — DB, engine, and Notion client are initialized on first use. This lets tests inject mocks by overwriting module globals without complex DI frameworks.

5. **Embedding as BLOB** — Float32 vectors are `struct.pack`-ed into raw bytes and stored in a SQLite BLOB column. This avoids a separate vector database dependency while keeping embeddings co-located with their metadata.

---

*For operational details, see [README.md](../README.md). For the Notion sync design specification, see [superpowers/specs/2026-07-07-notion-sync-design.md](superpowers/specs/2026-07-07-notion-sync-design.md).*
