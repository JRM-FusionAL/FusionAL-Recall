"""Migrate SOLVED-ISSUES.md into SQLite on first startup."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import RecallDB
    from .embeddings import EmbeddingEngine

log = logging.getLogger(__name__)

# Matches:  ## SI-001: Some title
_SI_HEADER = re.compile(r"^## (SI-\d+):\s+(.+)$", re.MULTILINE)
# Matches labelled fields like **Symptoms:** text
_FIELD = re.compile(
    r"\*\*(Symptoms|Root cause|Fix|Verified|Source|Tags):\*\*\s*(.+?)(?=\n\*\*|\Z)",
    re.DOTALL,
)


def _parse_issues(text: str) -> list[dict]:
    """Extract all SI-XXX blocks from markdown text.

    Returns a list of dicts with keys:
        si_id, title, symptoms, root_cause, fix, verified_at, source, tags
    """
    issues = []
    # Split at each SI header; first chunk is preamble (discarded)
    chunks = _SI_HEADER.split(text)
    # chunks = [preamble, si_id_1, title_1, body_1, si_id_2, title_2, body_2, ...]
    # Each block occupies 3 consecutive elements after the preamble
    i = 1  # skip preamble
    while i + 2 <= len(chunks):
        si_id = chunks[i].strip()
        title = chunks[i + 1].strip()
        body = chunks[i + 2]

        # Skip the TEMPLATE block
        if "XXX" in si_id:
            i += 3
            continue

        fields: dict[str, str] = {}
        for m in _FIELD.finditer(body):
            key = m.group(1).strip().lower().replace(" ", "_")
            val = m.group(2).strip()
            fields[key] = val

        # Parse Verified date — "2026-01 | Source: ..." format
        verified_raw = fields.get("verified", "")
        verified_at: str | None = None
        source = fields.get("source", "")
        if "|" in verified_raw:
            parts = verified_raw.split("|", 1)
            verified_at = parts[0].strip()
            # Source may be embedded after "Source:" in the verified field
            if "Source:" in parts[1]:
                source = parts[1].split("Source:", 1)[1].strip()
        elif verified_raw:
            verified_at = verified_raw

        # Tags — comma or space-separated
        tags_raw = fields.get("tags", "")
        tags = [t.strip() for t in re.split(r"[,\s]+", tags_raw) if t.strip()]

        issues.append(
            {
                "si_id": si_id,
                "title": title,
                "symptoms": fields.get("symptoms", ""),
                "root_cause": fields.get("root_cause", ""),
                "fix": fields.get("fix", ""),
                "verified_at": verified_at,
                "source": source,
                "tags": tags,
            }
        )
        i += 3

    return issues


def migrate_issues(
    db: "RecallDB",
    engine: "EmbeddingEngine",
    md_path: str | Path | None,
) -> int:
    """Load SOLVED-ISSUES.md into *db* if the database is empty.

    Returns the number of issues inserted (0 if already populated or file missing).
    """
    if db.count() > 0:
        log.info("migrate_issues: DB already populated, skipping migration.")
        return 0

    if md_path is None:
        log.warning("migrate_issues: SOLVED_ISSUES_PATH not set, skipping.")
        return 0

    path = Path(md_path)
    if not path.exists():
        log.warning("migrate_issues: %s not found, skipping.", path)
        return 0

    text = path.read_text(encoding="utf-8", errors="replace")
    issues_data = _parse_issues(text)

    if not issues_data:
        log.warning("migrate_issues: no SI entries found in %s", path)
        return 0

    log.info("migrate_issues: embedding %d issues…", len(issues_data))

    # Batch-embed all titles for speed
    titles = [d["title"] for d in issues_data]
    blobs = engine.embed_batch(titles)

    from .models import Issue

    inserted = 0
    for data, blob in zip(issues_data, blobs):
        issue = Issue(
            si_id=data["si_id"],
            title=data["title"],
            symptoms=data["symptoms"],
            root_cause=data["root_cause"],
            fix=data["fix"],
            source=data["source"],
            tags=data["tags"],
            verified_at=data["verified_at"],
            created_at=datetime.utcnow(),
            tier="personal",
            embedding=blob,
        )
        db.insert_issue(issue)
        inserted += 1

    log.info("migrate_issues: inserted %d issues.", inserted)
    return inserted
