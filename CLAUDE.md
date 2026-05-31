# CLAUDE.md — fusional-recall

Repo-specific instructions for Claude Code sessions inside this project.
Global rules in `C:\Users\puddi\.claude\CLAUDE.md` apply first.

## Project Identity

**fusional-recall** is a standalone FastMCP server that wraps the SOLVED-ISSUES registry
with semantic search via sentence-transformers. It is NOT part of fusional-knowledge-base (SI-009).

Port: **8107**
DB:   `./recall.db` (local) or `/data/recall.db` (Docker)

## Recall Protocol (this project)

Before debugging any error in this repo:
1. State "Recall check: searching SOLVED-ISSUES for <fingerprint>"
2. Check `C:\Users\puddi\Projects\fusional-knowledge-base\05-RECALL\SOLVED-ISSUES.md`
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
| `remember`   | Log new SI entry, auto-assigns SI-ID       |
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
