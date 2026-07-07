# Notion Sync for FusionAL-Recall — Design

**Date:** 2026-07-07
**Status:** Approved

## Problem

The solved-issues registry forked. Writes moved to the Notion Solved Issues DB
(data source `836a9fe2-738d-4fdf-90a5-4364e1b36f1f`) when the
`recall-and-handoff` skill was repointed there in June 2026. Recall still
indexes `SOLVED-ISSUES.md`, which stopped receiving entries on 2026-07-01.
Everything logged since then is searchable in Notion but invisible to the
`recall` semantic search.

## Decision

Recall indexes Notion directly. Notion is canonical; `recall.db` (SQLite) is a
derived semantic index. The markdown migration stays only as an empty-DB
seeding fallback.

## Architecture

### New module: `recall/notion_sync.py`

- **`NotionClient`** — thin `httpx` wrapper over the Notion REST API
  (no new dependency; `httpx` ships with `mcp`). Two operations:
  - `query_all_pages()` — paginated POST to
    `/v1/data_sources/{id}/query` (fall back to `/v1/databases/{id}/query`
    if the data-source endpoint is unavailable on the account's API
    version), returning all page objects.
  - `create_page(properties)` — POST `/v1/pages` for dual-write from
    `remember`.
  - Auth via `NOTION_TOKEN` bearer header. All calls have explicit
    timeouts (10 s) and raise `NotionSyncError` on failure.

- **`sync_from_notion(db, engine, client) -> int`** — pulls all pages, maps
  each to the existing `Issue` model, embeds **only new or changed pages**
  (keyed on Notion page ID + `last_edited_time`), upserts into SQLite.
  Returns count of inserted/updated rows. Never raises to callers on
  Notion/network failure — logs a warning and returns 0.

### Field mapping (Notion page → Issue)

| Notion property | Issue field |
|---|---|
| `Issue` (title) | `title` |
| `Solution` (rich text) | parsed → `symptoms`, `root_cause`, `fix` |
| `Project` | `source` |
| `Tags` (multi-select) | `tags` |
| `Severity` | appended to `tags` as `severity:<value>` |
| page `id` | `notion_page_id` |
| page `last_edited_time` | `notion_edited_at` |
| page `created_time` | `created_at` |

`Solution` parsing: split on the `Symptoms:`, `Root cause:`, `Fix:` labels the
logging skill writes (case-insensitive, tolerant of `\n` or ` ` after the
colon). If no labels are present, the whole text goes into `fix` and
`symptoms`/`root_cause` stay empty.

Embedding input stays consistent with the current code: for synced entries the
embedded text is `f"{title} {symptoms} {root_cause}"` (matching `remember`).

### SI-ID assignment for synced rows

- If the Notion title or solution text contains a legacy `SI-\d+` ID, reuse it.
- Otherwise `si_id = "N-" + first 8 hex chars of the page ID` — stable across
  syncs, no collision with the `SI-\d+` sequence.

### Schema migration (`recall/db.py`)

Add two nullable columns to `issues`:

- `notion_page_id TEXT UNIQUE`
- `notion_edited_at TEXT`

Applied idempotently at `RecallDB.__init__` via
`ALTER TABLE ... ADD COLUMN` guarded by a `PRAGMA table_info` check. Existing
rows are untouched (columns stay NULL). Upsert for synced rows keys on
`notion_page_id`, not `si_id`.

### `remember` becomes dual-write (`recall/server.py`)

1. Attempt `NotionClient.create_page(...)` with the mapped properties
   (`Issue`, `Solution` composed as `Symptoms: ...\nRoot cause: ...\nFix: ...`,
   `Project` from `source`, `Tags`, `Severity` default `Medium`).
2. Insert locally regardless of Notion outcome, storing `notion_page_id` when
   the create succeeded.
3. Response gains `"notion_synced": true|false`. Logging never blocks on
   Notion availability.

### Sync cadence (`recall/server.py`)

- `_on_startup()`: existing markdown migration first (empty-DB fallback),
  then `sync_from_notion(...)` if `NOTION_TOKEN` is set.
- Background daemon thread re-syncs every `NOTION_SYNC_INTERVAL` seconds
  (default 3600; `0` disables the thread). Failures log and skip the cycle.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `NOTION_TOKEN` | unset | Notion internal-integration token. Unset → sync disabled, server behaves as today. |
| `NOTION_DATA_SOURCE_ID` | `836a9fe2-738d-4fdf-90a5-4364e1b36f1f` | Solved Issues data source. |
| `NOTION_SYNC_INTERVAL` | `3600` | Seconds between background syncs; `0` disables. |

Deployment: add `NOTION_TOKEN` to the systemd unit drop-in (reuse the existing
token from `~/.hermes/.env` / `~/Projects/FusionAL/.env` after verifying it
can read the Solved Issues DB).

## Error handling

- Notion unreachable at startup → warn, serve existing index.
- Notion unreachable during `remember` → local write succeeds,
  `notion_synced: false`.
- Malformed page (missing title) → skip page, log warning, continue sync.
- Sync never crashes the server; the daemon thread catches all exceptions.

## Testing (TDD, mocked HTTP — no real model, matching existing test style)

1. Solution-text parsing: labeled, unlabeled, mixed-case, multiline.
2. Field mapping including severity→tag and legacy SI-ID extraction.
3. Upsert idempotency: same page synced twice → one row; changed
   `last_edited_time` → row updated and re-embedded.
4. Notion-down: `sync_from_notion` returns 0, no exception; startup completes.
5. `remember` dual-write: Notion success stores page ID; Notion failure still
   inserts locally with `notion_synced: false`.
6. Schema migration: opening an existing DB adds columns without data loss.

## Out of scope

- Deleting local rows when Notion pages are archived (append-only registry).
- Two-way edit sync (Notion is canonical for edits).
- Indexing any Notion database other than Solved Issues.
- Fixing the hatchling packaging config (separate one-line follow-up: add
  `fastembed` to `pyproject.toml` and a `[tool.hatch.build.targets.wheel]
  packages = ["recall"]` entry).
