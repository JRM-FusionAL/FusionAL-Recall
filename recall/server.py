"""FusionAL Recall — FastMCP server entry point."""

from __future__ import annotations

import hmac
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP

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
# Loopback by default. Containers override this to 0.0.0.0 inside their own
# network namespace; the host-side exposure is then set by the port publish
# (docker-compose binds 127.0.0.1:8107).
HOST = os.getenv("RECALL_HOST", "127.0.0.1")
PORT = int(os.getenv("RECALL_PORT", "8107"))
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATA_SOURCE_ID = os.getenv(
    "NOTION_DATA_SOURCE_ID", "836a9fe2-738d-4fdf-90a5-4364e1b36f1f"
)
NOTION_SYNC_INTERVAL = int(os.getenv("NOTION_SYNC_INTERVAL", "3600"))

# ------------------------------------------------------------------
# Write-tool authentication
#
# Optional. When a token is configured, `remember` requires
# "Authorization: Bearer <token>" on the HTTP request. The token comes from
# RECALL_AUTH_TOKEN, or from the file named by RECALL_AUTH_TOKEN_FILE (keep it
# mode 600). When neither is set, writes are accepted without a token, which
# keeps loopback-only local use working; startup logs a warning instead.
# Read tools are not gated by this token.
# ------------------------------------------------------------------

AUTH_TOKEN_ENV = "RECALL_AUTH_TOKEN"
AUTH_TOKEN_FILE_ENV = "RECALL_AUTH_TOKEN_FILE"


def _configured_write_token() -> str | None:
    """Return the write token, or None when auth is not configured.

    Read on each call so a rotated token file takes effect without a restart.
    A configured token file that cannot be read raises, so the server fails
    closed instead of silently dropping authentication.
    """
    token_file = os.getenv(AUTH_TOKEN_FILE_ENV, "").strip()
    if token_file:
        path = Path(token_file)
        try:
            mode = path.stat().st_mode
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"{AUTH_TOKEN_FILE_ENV} is set but unreadable: {exc.strerror}") from exc
        if stat.S_ISREG(mode) and mode & 0o077:
            log.warning("%s is group/world accessible; run chmod 600 on it", AUTH_TOKEN_FILE_ENV)
        if token:
            return token
    token = os.getenv(AUTH_TOKEN_ENV, "").strip()
    return token or None


def _warn_if_writes_unauthenticated() -> None:
    if _configured_write_token() is None:
        log.warning(
            "No %s or %s set: remember() accepts unauthenticated writes. "
            "Keep RECALL_HOST on loopback, or set a write token.",
            AUTH_TOKEN_ENV,
            AUTH_TOKEN_FILE_ENV,
        )


def _require_write_auth(ctx: Context) -> None:
    """Raise PermissionError unless the HTTP caller presents the write token.

    ctx is None-safe: in-process calls and stdio transport have no HTTP request
    and are local to the host, so they are not checked.
    """
    expected = _configured_write_token()
    if expected is None:
        return
    request = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
        except ValueError:
            request = None
    if request is None:
        return
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    presented = presented.strip()
    if scheme.lower() != "bearer" or not presented or not hmac.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    ):
        raise PermissionError("remember requires a valid bearer token")


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
    ctx: Context = None,
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

    Raises:
        PermissionError: a write token is configured and the request did not
            present it as a bearer token.
    """
    from .models import Issue, RememberResult

    _require_write_auth(ctx)
    db = get_db()
    engine = get_engine()

    si_id = db.next_si_id()
    blob = engine.embed(f"{title} {symptoms} {root_cause}")

    # Local-first by design. Agents must not spend a request round-trip on
    # Notion: recall.db is the operational index, and the sync cadence pushes
    # unsynced rows to Notion asynchronously. This also means a Notion outage
    # cannot delay or block logging a solved issue.
    notion_page_id: str | None = None

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
    db.mark_notion_sync_pending(si_id)

    result = RememberResult(
        si_id=si_id,
        title=title,
        created_at=issue.created_at,
        tier=tier,
        message=f"Logged {si_id}: {title} — queued for Notion sync",
        # Always "pending" here: the Notion write is deliberately deferred to
        # the sync cadence, so it has not happened yet and saying so is the
        # honest answer. The previous field reported `notion_synced: false`
        # on every successful call, which reads as a failed write rather than
        # a deferred one — see the note on RememberResult.notion_sync.
        notion_sync="pending",
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
        from .notion_sync import sync_from_notion, sync_to_notion
        synced = sync_from_notion(db, engine, client)
        pushed = sync_to_notion(db, client)
        log.info("startup: notion sync pulled %d issue(s), pushed %d local issue(s)", synced, pushed)
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
        from .notion_sync import sync_from_notion, sync_to_notion
        while True:
            time.sleep(NOTION_SYNC_INTERVAL)
            try:
                db = get_db()
                sync_from_notion(db, get_engine(), client)
                sync_to_notion(db, client)
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
    _warn_if_writes_unauthenticated()
    _on_startup()
    _start_sync_thread()
    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
