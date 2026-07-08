"""FusionAL Recall — FastMCP server entry point."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
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
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATA_SOURCE_ID = os.getenv(
    "NOTION_DATA_SOURCE_ID", "836a9fe2-738d-4fdf-90a5-4364e1b36f1f"
)
NOTION_SYNC_INTERVAL = int(os.getenv("NOTION_SYNC_INTERVAL", "3600"))

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


_notion: "NotionClient | None" = None


def get_notion():
    """NotionClient singleton, or None when NOTION_TOKEN is unset."""
    global _notion
    if _notion is None and NOTION_TOKEN:
        from .notion_sync import NotionClient
        _notion = NotionClient(NOTION_TOKEN, NOTION_DATA_SOURCE_ID)
    return _notion


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
    from .notion_sync import build_notion_properties

    db = get_db()
    engine = get_engine()

    si_id = db.next_si_id()
    blob = engine.embed(f"{title} {symptoms} {root_cause}")

    # Notion is the canonical registry — write there first, but never
    # let a Notion outage block local logging.
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


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

from starlette.requests import Request
from starlette.responses import JSONResponse


@app.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Simple health check endpoint for monitoring."""
    return JSONResponse({"status": "ok", "service": "fusional-recall", "port": PORT})


def main() -> None:  # pragma: no cover
    _on_startup()
    _start_sync_thread()
    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
