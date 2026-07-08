# CLAUDE.md — fusional-recall

Repo-specific instructions for Claude Code sessions inside this project.
Global rules in `~/.claude/CLAUDE.md` apply first.

## Project Identity

**fusional-recall** is a standalone FastMCP server providing semantic search over the
solved-issues registry. The **Notion Solved Issues DB is canonical** (data source
`836a9fe2-738d-4fdf-90a5-4364e1b36f1f`); `recall.db` is a derived index synced from it
at startup and hourly. `SOLVED-ISSUES.md` remains only as an empty-DB seed fallback.
Embeddings via fastembed (ONNX). See `docs/superpowers/specs/2026-07-07-notion-sync-design.md`.

Port: **8107**
DB:   `./recall.db` (local) or `/data/recall.db` (Docker)

## Config (env / `.env` in repo root — never commit `.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `NOTION_TOKEN` | unset | Notion integration token; unset disables sync |
| `NOTION_DATA_SOURCE_ID` | `836a9fe2-...36f1f` | Solved Issues data source |
| `NOTION_SYNC_INTERVAL` | `3600` | Seconds between background syncs; 0 disables |

## Recall Protocol (this project)

Before debugging any error in this repo:
1. State "Recall check: searching solved issues for <fingerprint>"
2. Call the `recall` MCP tool (localhost:8107) — or query the Notion Solved Issues DB
3. State outcome

## Auto-Log Triggers (this project)

Log a new SI entry whenever:
- migration regex fails to parse a valid SI block
- embedding model fails to load (model name drift, network, HW)
- cosine similarity returns unexpected values (float packing bug)
- FastMCP version change breaks tool registration
- DB path or env var silently falls back to wrong default

## Dev Commands

```bash
# Install deps (WSL or native Python)
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Run tests
pytest tests/ -v --cov=recall --cov-report=term-missing

# Start server (reads .env)
python -m recall.server

# Docker
docker compose up --build
```

## MCP Tool Summary

| Tool         | Purpose                                    |
|--------------|--------------------------------------------|
| `recall`     | Semantic search — returns ranked SI list   |
| `remember`   | Log new SI entry to Notion (canonical) + local index |
| `list_recent`| List N newest issues                       |
| `verify`     | Fetch specific issue by SI-ID              |
| `get`        | Alias for `verify`                         |

## Key Files

- `recall/server.py`   — FastMCP app, tool definitions, startup hook
- `recall/db.py`       — SQLite persistence, cosine similarity
- `recall/migrate.py`  — SOLVED-ISSUES.md parser and migration
- `recall/embeddings.py` — SentenceTransformer wrapper, struct.pack float32
- `recall/models.py`   — Pydantic v2 models
- `tests/test_server.py` — Full test suite (no real model loaded)
