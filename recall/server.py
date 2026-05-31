"""FusionAL Recall — FastMCP server entry point."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

DB_PATH = os.getenv("RECALL_DB_PATH", "./recall.db")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
DEFAULT_TIER = os.getenv("DEFAULT_TIER", "personal")
SOLVED_ISSUES_PATH = os.getenv("SOLVED_ISSUES_PATH", None)
HOST = os.getenv("RECALL_HOST", "0.0.0.0")
PORT = int(os.getenv("RECALL_PORT", "8107"))

# ------------------------------------------------------------------
# Singletons (initialised lazily on first request so tests can inject mocks)
# ------------------------------------------------------------------

_db: "RecallDB | None" = None
_engine: "EmbeddingEngine | None" = None


def get_db():
    global _db
    if _db is None:
        from .db import RecallDB
        _db = RecallDB(DB_PATH)
    return _db


def get_engine():
    global _engine
    if _engine is None:
        from .embeddings import EmbeddingEngine
        _engine = EmbeddingEngine(EMBEDDING_MODEL)
    return _engine


# ------------------------------------------------------------------
# FastMCP app (v3 API — host/port passed to constructor)
# ------------------------------------------------------------------

app = FastMCP(
    "fusional-recall",
    host=HOST,
    port=PORT,
)


@app.tool()
def recall(
    query: str,
    tier: Optional[str] = None,
    limit: int = 5,
) -> list[dict]:
    """Semantic search over the SOLVED-ISSUES registry.

    Args:
        query: Natural-language description of the error or situation.
        tier:  Filter by access tier — 'personal', 'project', or 'public'.
               Omit to search all tiers.
        limit: Maximum results to return (default 5).

    Returns:
        List of matching issues ranked by cosine similarity.
    """
    db = get_db()
    engine = get_engine()
    blob = engine.embed(query)
    results = db.search_by_embedding(blob, limit=limit, tier=tier)
    return [r.model_dump() for r in results]


@app.tool()
def remember(
    title: str,
    symptoms: str,
    root_cause: str,
    fix: str,
    source: str = "",
    tags: list[str] | None = None,
    tier: str = "personal",
) -> dict:
    """Log a new solved issue to the registry and assign the next SI-ID.

    Args:
        title:      One-line summary of the issue.
        symptoms:   What the agent observed.
        root_cause: What was actually wrong.
        fix:        Exact steps that resolved the issue.
        source:     Optional — session name, repo, or ticket reference.
        tags:       Optional list of searchable labels.
        tier:       Access tier — 'personal' (default), 'project', or 'public'.

    Returns:
        RememberResult with the new SI-ID and confirmation message.
    """
    from .models import Issue, RememberResult

    db = get_db()
    engine = get_engine()

    si_id = db.next_si_id()
    blob = engine.embed(f"{title} {symptoms} {root_cause}")

    issue = Issue(
        si_id=si_id,
        title=title,
        symptoms=symptoms,
        root_cause=root_cause,
        fix=fix,
        source=source,
        tags=tags or [],
        created_at=datetime.utcnow(),
        tier=tier,
        embedding=blob,
    )
    db.insert_issue(issue)

    result = RememberResult(
        si_id=si_id,
        title=title,
        created_at=issue.created_at,
        tier=tier,
        message=f"Logged {si_id}: {title}",
    )
    return result.model_dump()


@app.tool()
def list_recent(n: int = 10, tier: Optional[str] = None) -> list[dict]:
    """List the N most recently added issues (default 10).

    Args:
        n:    Number of issues to return.
        tier: Optional tier filter.

    Returns:
        List of Issue dicts ordered newest-first.
    """
    db = get_db()
    issues = db.list_recent_issues(n=n, tier=tier)
    return [
        i.model_dump(exclude={"embedding"})
        for i in issues
    ]


@app.tool()
def verify(si_id: str) -> dict:
    """Retrieve a specific issue by its SI-ID and confirm it exists.

    Args:
        si_id: Identifier such as 'SI-001'.

    Returns:
        The full Issue record, or an error dict if not found.
    """
    db = get_db()
    issue = db.get_issue_by_id(si_id)
    if issue is None:
        return {"error": f"{si_id} not found in registry"}
    return issue.model_dump(exclude={"embedding"})


@app.tool()
def get(si_id: str) -> dict:
    """Alias for verify — fetch a single issue by SI-ID.

    Args:
        si_id: Identifier such as 'SI-007'.

    Returns:
        The full Issue record, or an error dict if not found.
    """
    return verify(si_id)


# ------------------------------------------------------------------
# Startup — run migration
# ------------------------------------------------------------------

def _on_startup() -> None:
    """Migrate SOLVED-ISSUES.md on first run (no-op if DB already populated)."""
    from .migrate import migrate_issues
    db = get_db()
    engine = get_engine()
    inserted = migrate_issues(db, engine, SOLVED_ISSUES_PATH)
    if inserted:
        log.info("startup: migrated %d issues from %s", inserted, SOLVED_ISSUES_PATH)
    else:
        log.info("startup: DB ready with %d issues", db.count())


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:  # pragma: no cover
    _on_startup()
    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
