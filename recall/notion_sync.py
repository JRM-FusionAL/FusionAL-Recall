"""Sync the Notion Solved Issues database into the local recall index.

Notion is the canonical registry; recall.db is a derived semantic index.
See docs/superpowers/specs/2026-07-07-notion-sync-design.md.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import RecallDB
    from .embeddings import EmbeddingEngine

from .models import Issue

log = logging.getLogger(__name__)

_SI_ID = re.compile(r"\bSI-\d{3}\b")


def parse_solution(text: str) -> dict[str, str]:
    """Split a combined Solution text into symptoms / root_cause / fix.

    The logging skill writes 'Symptoms: ...', 'Root cause: ...', 'Fix: ...'
    but real entries vary: labels may be newline-separated or flow inline
    within sentences, in any case. Text with no labels at all should land
    whole in 'fix'. Always returns all three keys (missing sections = '').
    """
    parts = {"symptoms": "", "root_cause": "", "fix": ""}
    found: list[tuple[int, int, str]] = []
    for key, pat in (("symptoms", r"symptoms"), ("root_cause", r"root\s+cause"), ("fix", r"fix")):
        m = re.search(rf"(?i)\b{pat}\s*:\s*", text)
        if m:
            found.append((m.start(), m.end(), key))
    if not found:
        parts["fix"] = text.strip()
        return parts
    found.sort()
    for i, (_, end, key) in enumerate(found):
        nxt = found[i + 1][0] if i + 1 < len(found) else len(text)
        parts[key] = text[end:nxt].strip()
    return parts


def derive_si_id(page_id: str, title: str) -> str:
    """Reuse a legacy SI-ID only when the *title* carries one.

    Body text must not be trusted for identity: an entry that merely
    mentions "see SI-007" would otherwise overwrite the local SI-007 row
    (INSERT OR REPLACE keys on si_id).
    """
    m = _SI_ID.search(title)
    if m:
        return m.group(0)
    # Full hex, not a prefix: Notion page IDs are time-ordered, so short
    # prefixes collide across pages created near each other.
    return "N-" + page_id.replace("-", "")


def _plain_text(rich: list[dict]) -> str:
    return "".join(r.get("plain_text", "") for r in rich)


def _select_name(prop: dict) -> str:
    return (prop.get("select") or {}).get("name", "") if prop else ""


def map_page_to_issue(page: dict) -> Issue | None:
    """Map a Notion page object to an Issue. Returns None for pages with no title."""
    props = page.get("properties", {})
    title = _plain_text(props.get("Issue", {}).get("title", []))
    if not title.strip():
        return None
    solution = _plain_text(props.get("Solution", {}).get("rich_text", []))
    parsed = parse_solution(solution)
    tags = [t["name"] for t in props.get("Tags", {}).get("multi_select", []) or []]
    severity = _select_name(props.get("Severity", {}))
    if severity:
        tags.append(f"severity:{severity}")
    created_raw = page.get("created_time", "")
    try:
        created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    except ValueError:
        created_at = datetime.now(timezone.utc)
    return Issue(
        si_id=derive_si_id(page["id"], title),
        title=title.strip(),
        symptoms=parsed["symptoms"],
        root_cause=parsed["root_cause"],
        fix=parsed["fix"],
        source=_select_name(props.get("Project", {})),
        tags=tags,
        created_at=created_at,
        tier="personal",
        embedding=None,
        notion_page_id=page["id"],
        notion_edited_at=page.get("last_edited_time"),
    )


class NotionSyncError(Exception):
    """Raised when the Notion API returns an error response."""


class NotionClient:
    """Minimal Notion REST client — query + create only, no SDK dependency."""

    BASE = "https://api.notion.com/v1"

    def __init__(self, token: str, data_source_id: str, timeout: float = 10.0) -> None:
        self._token = token
        self._ds_id = data_source_id
        self._timeout = timeout

    def _post(self, path: str, payload: dict, version: str) -> dict:
        import httpx

        resp = httpx.post(
            f"{self.BASE}{path}",
            json=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise NotionSyncError(f"POST {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _query_once(self, payload: dict) -> dict:
        # Data-source endpoint (2025 API); fall back to classic database query.
        try:
            return self._post(f"/data_sources/{self._ds_id}/query", payload, "2025-09-03")
        except NotionSyncError:
            return self._post(f"/databases/{self._ds_id}/query", payload, "2022-06-28")

    def query_all_pages(self) -> list[dict]:
        pages: list[dict] = []
        cursor: str | None = None
        while True:
            payload: dict = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._query_once(payload)
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")

    def create_page(self, properties: dict) -> str:
        payload: dict = {
            "parent": {"type": "data_source_id", "data_source_id": self._ds_id},
            "properties": properties,
        }
        try:
            data = self._post("/pages", payload, "2025-09-03")
        except NotionSyncError:
            payload["parent"] = {"database_id": self._ds_id}
            data = self._post("/pages", payload, "2022-06-28")
        return data["id"]


def _chunk_rich_text(content: str, limit: int = 2000) -> list[dict]:
    chunks = [content[i : i + limit] for i in range(0, len(content), limit)] or [""]
    return [{"text": {"content": c}} for c in chunks]


def build_notion_properties(
    title: str,
    solution: str,
    project: str,
    tags: list[str],
    severity: str = "Medium",
) -> dict:
    """Properties payload for creating a Solved Issues page."""
    return {
        "Issue": {"title": _chunk_rich_text(title)},
        "Solution": {"rich_text": _chunk_rich_text(solution)},
        "Project": {"select": {"name": project or "General"}},
        "Tags": {"multi_select": [{"name": t} for t in tags]},
        "Severity": {"select": {"name": severity}},
    }


def sync_from_notion(db: "RecallDB", engine: "EmbeddingEngine", client: NotionClient) -> int:
    """Incrementally upsert Notion pages into the local index.

    Returns the number of rows inserted/updated. Never raises on
    Notion/network failure — the existing index keeps serving.
    """
    try:
        pages = client.query_all_pages()
    except Exception as exc:
        log.warning("notion sync: query failed, keeping existing index: %s", exc)
        return 0

    state = db.notion_sync_state()
    changed = 0
    for page in pages:
        page_id = page.get("id", "")
        edited = page.get("last_edited_time") or ""
        if page_id and edited and state.get(page_id) == edited:
            continue
        issue = map_page_to_issue(page)
        if issue is None:
            log.warning("notion sync: skipping page %s (no title)", page_id)
            continue
        issue.embedding = engine.embed(f"{issue.title} {issue.symptoms} {issue.root_cause}")
        db.insert_issue(issue)
        changed += 1
    log.info("notion sync: %d page(s) upserted, %d total in index", changed, db.count())
    return changed
