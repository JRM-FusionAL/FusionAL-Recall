"""FusionAL Recall MCP server — semantic search over SOLVED-ISSUES registry."""

import os
from datetime import datetime
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .db import RecallDB
from .embeddings import EmbeddingEngine
from .migrate import parse_solved_issues
from .models import Issue

mcp = FastMCP(
    "fusional-recall",
    host=os.getenv("RECALL_HOST", "0.0.0.0"),
    port=int(os.getenv("RECALL_PORT", "8107")),
)

_db: Optional[RecallDB] = None
_embedder: Optional[EmbeddingEngine] = None


def _get_db() -> RecallDB:
    global _db
    if _db is None:
        db_path = os.getenv("RECALL_DB_PATH", "./recall.db")
        _db = RecallDB(db_path)
        _migrate_if_needed(_db)
    return _db


def _get_embedder() -> EmbeddingEngine:
    global _embedder
    if _embedder is None:
        model = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        _embedder = EmbeddingEngine(model)
    return _embedder


def _migrate_if_needed(db: RecallDB) -> None:
    """Load SOLVED-ISSUES.md into the DB on first run (when DB is empty)."""
    if db.count() > 0:
        return
    si_path = os.getenv("SOLVED_ISSUES_PATH", "")
    if not si_path or not os.path.exists(si_path):
        return
    issues = parse_solved_issues(si_path)
    embedder = _get_embedder()
    for issue in issues:
        combined = f"{issue.title}. {issue.symptoms}. {issue.root_cause}"
        issue.embedding = embedder.embed(combined)
        db.insert_issue(issue)


@mcp.tool()
def recall(query: str, limit: int = 5, tier: str = "") -> str:
    """Search the solved-issues registry by natural language query."""
    db = _get_db()
    embedder = _get_embedder()
    embedding = embedder.embed(query)
    results = db.search_by_embedding(embedding, limit=limit, tier=tier or None)
    if not results:
        return "No matching issues found."
    lines = []
    for r in results:
        lines.append(
            f"[{r.si_id}] {r.title} (score: {r.similarity:.2f})\n"
            f"  Symptoms: {r.symptoms}\n"
            f"  Fix: {r.fix}"
        )
    return "\n\n".join(lines)


@mcp.tool()
def remember(
    title: str,
    symptoms: str,
    root_cause: str,
    fix: str,
    source: str,
    tags: str = "",
    tier: str = "personal",
    verified_at: str = "",
) -> str:
    """Store a new solved issue in the registry."""
    db = _get_db()
    embedder = _get_embedder()
    si_id = db.get_next_si_id()
    combined = f"{title}. {symptoms}. {root_cause}"
    embedding = embedder.embed(combined)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    issue = Issue(
        si_id=si_id,
        title=title,
        symptoms=symptoms,
        root_cause=root_cause,
        fix=fix,
        source=source,
        tags=tag_list,
        verified_at=verified_at or None,
        tier=tier,
        embedding=embedding,
    )
    db.insert_issue(issue)
    return f"Stored {si_id}: {title}"


@mcp.tool()
def list_recent(n: int = 10, tier: str = "") -> str:
    """List the N most recently added solved issues."""
    db = _get_db()
    issues = db.list_recent_issues(n=n, tier=tier or None)
    if not issues:
        return "No issues in registry."
    lines = [f"[{i.si_id}] {i.title} ({i.tier})" for i in issues]
    return "\n".join(lines)


@mcp.tool()
def verify(si_id: str) -> str:
    """Mark a solved issue as verified today."""
    db = _get_db()
    issue = db.get_issue_by_id(si_id)
    if not issue:
        return f"{si_id} not found."
    issue.verified_at = datetime.utcnow().strftime("%Y-%m")
    db.insert_issue(issue)
    return f"{si_id} verified at {issue.verified_at}."


@mcp.tool()
def get(si_id: str) -> str:
    """Retrieve a single solved issue by ID (e.g. SI-001)."""
    db = _get_db()
    issue = db.get_issue_by_id(si_id)
    if not issue:
        return f"{si_id} not found."
    tags = ", ".join(issue.tags) if issue.tags else "none"
    return (
        f"[{issue.si_id}] {issue.title}\n"
        f"Symptoms: {issue.symptoms}\n"
        f"Root cause: {issue.root_cause}\n"
        f"Fix: {issue.fix}\n"
        f"Source: {issue.source}\n"
        f"Tags: {tags}\n"
        f"Verified: {issue.verified_at or 'unverified'} | Tier: {issue.tier}"
    )


def main():
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
