"""SQLite persistence layer for FusionAL Recall."""

from __future__ import annotations

import math
import sqlite3
import struct
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .models import Issue, QueryResult


class RecallDB:
    """Thin SQLite wrapper — no ORM, just raw SQL + struct-packed float32 blobs."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS issues (
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
                embedding   BLOB
            );
            CREATE INDEX IF NOT EXISTS idx_tier ON issues(tier);
            CREATE INDEX IF NOT EXISTS idx_created ON issues(created_at DESC);
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def insert_issue(self, issue: Issue) -> None:
        tags_str = ",".join(issue.tags)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO issues
                (si_id, title, symptoms, root_cause, fix, source,
                 tags, verified_at, created_at, tier, embedding)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
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
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_issue_by_id(self, si_id: str) -> Optional[Issue]:
        row = self._conn.execute(
            "SELECT * FROM issues WHERE si_id = ?", (si_id,)
        ).fetchone()
        return self._row_to_issue(row) if row else None

    def list_recent_issues(
        self, n: int = 10, tier: Optional[str] = None
    ) -> List[Issue]:
        if tier:
            rows = self._conn.execute(
                "SELECT * FROM issues WHERE tier = ? ORDER BY created_at DESC LIMIT ?",
                (tier, n),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM issues ORDER BY created_at DESC LIMIT ?", (n,)
            ).fetchall()
        return [self._row_to_issue(r) for r in rows]

    def search_by_embedding(
        self, embedding_bytes: bytes, limit: int = 5, tier: Optional[str] = None
    ) -> List[QueryResult]:
        """Return issues ranked by cosine similarity to *embedding_bytes*."""
        query_vec = self._unpack(embedding_bytes)

        if tier:
            rows = self._conn.execute(
                "SELECT * FROM issues WHERE tier = ? AND embedding IS NOT NULL", (tier,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM issues WHERE embedding IS NOT NULL"
            ).fetchall()

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            stored_vec = self._unpack(bytes(row["embedding"]))
            sim = self._cosine_similarity(query_vec, stored_vec)
            scored.append((sim, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, row in scored[:limit]:
            tags = [t for t in row["tags"].split(",") if t]
            results.append(
                QueryResult(
                    si_id=row["si_id"],
                    title=row["title"],
                    symptoms=row["symptoms"],
                    root_cause=row["root_cause"],
                    fix=row["fix"],
                    source=row["source"] or "",
                    tags=tags,
                    similarity=round(sim, 4),
                    tier=row["tier"],
                )
            )
        return results

    def next_si_id(self) -> str:
        """Return the next available SI-XXX identifier."""
        row = self._conn.execute(
            "SELECT si_id FROM issues ORDER BY si_id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "SI-001"
        last = row["si_id"]  # e.g. "SI-029"
        try:
            num = int(last.split("-")[1]) + 1
        except (IndexError, ValueError):
            num = 1
        return f"SI-{num:03d}"

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _unpack(blob: bytes) -> tuple[float, ...]:
        n = len(blob) // 4
        return struct.unpack(f"{n}f", blob)

    @staticmethod
    def _cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def _row_to_issue(self, row: sqlite3.Row) -> Issue:
        tags = [t for t in row["tags"].split(",") if t]
        created = row["created_at"]
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except ValueError:
                created = datetime.utcnow()
        return Issue(
            si_id=row["si_id"],
            title=row["title"],
            symptoms=row["symptoms"],
            root_cause=row["root_cause"],
            fix=row["fix"],
            source=row["source"] or "",
            tags=tags,
            verified_at=row["verified_at"],
            created_at=created,
            tier=row["tier"],
            embedding=bytes(row["embedding"]) if row["embedding"] else None,
        )
