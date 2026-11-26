"""Synchronize the local recall index with its Notion mirror.

`recall.db` is the operational source of truth. New agent entries are written
locally first and explicitly marked for asynchronous push to Notion; the same
cadence pulls Notion-side updates back into the local index.
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

_SI_ID = re.compile(r"\bSI-\d{3,}\b")
# PI- counts only as a title prefix; elsewhere it is read as a mention.
_PI_PREFIX = re.compile(r"^\s*(PI-\d{3,})\b")


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


def derive_si_id(page_id: str, title: str, local_id: str | None = None) -> str:
    """Pick a page's ID: from its *title*, else the linked row's, else N-<hex>.

    Body text must not be trusted for identity: an entry that merely
    mentions "see SI-007" would otherwise overwrite the local SI-007 row
    (INSERT OR REPLACE keys on si_id).
    """
    pi = _PI_PREFIX.match(title)
    if pi:
        return pi.group(1)
    m = _SI_ID.search(title)
    if m:
        return m.group(0)
    if local_id:
        # Many pages carry no ID in their title (every PI- page, for one).
        return local_id
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

    def create_page(self, properties: dict) -> tuple[str, str]:
        """Create a page and return ``(page_id, last_edited_time)``."""
        payload: dict = {
            "parent": {"type": "data_source_id", "data_source_id": self._ds_id},
            "properties": properties,
        }
        try:
            data = self._post("/pages", payload, "2025-09-03")
        except NotionSyncError as exc:
            # Legacy fallback for workspaces still on database_id parents. When
            # _ds_id is a data source id the legacy attempt always 404s, so its
            # error would mask the real one -- re-raise the original instead.
            payload["parent"] = {"database_id": self._ds_id}
            try:
                data = self._post("/pages", payload, "2022-06-28")
            except NotionSyncError:
                raise exc from None
        return data["id"], data.get("last_edited_time") or ""


def _chunk_rich_text(content: str, limit: int = 2000) -> list[dict]:
    chunks = [content[i : i + limit] for i in range(0, len(content), limit)] or [""]
    return [{"text": {"content": c}} for c in chunks]


def _select_option_name(value: str, fallback: str) -> str:
    """Sanitise a select/multi-select option name.

    Notion rejects commas in option names with a 400 validation_error, and
    callers hand this free text -- `remember`'s `source` becomes Project. A
    single comma used to fail the whole page create, so scrub instead.
    """
    cleaned = value.replace(",", " ").strip()[:100].strip()
    return cleaned or fallback


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
        "Project": {"select": {"name": _select_option_name(project, "General")}},
        "Tags": {
            "multi_select": [
                {"name": name}
                for name in (_select_option_name(t, "") for t in tags)
                if name
            ]
        },
        "Severity": {"select": {"name": _select_option_name(severity, "Medium")}},
    }


def _notion_title(issue: Issue) -> str:
    return issue.title if issue.si_id in issue.title else f"{issue.si_id} — {issue.title}"


def _notion_solution(issue: Issue) -> str:
    return f"Symptoms: {issue.symptoms}\nRoot cause: {issue.root_cause}\nFix: {issue.fix}"


def _merge_with_local(remote: Issue, local: Issue) -> Issue:
    """Apply a pulled page to its linked row without losing local data.

    Notion has no tier or verification date, and its creation time is the
    page's, not the entry's. A push also rewrites some values (ID prefix on
    the title, commas stripped from options, default severity), so a value
    that still equals the pushed form of the local one was not edited.
    """
    keep: dict = {
        "tier": local.tier,
        "created_at": local.created_at,
        "verified_at": local.verified_at,
    }
    if remote.title == _notion_title(local).strip():
        keep["title"] = local.title
    # Option names are compared without case: Notion reuses an existing one's.
    if remote.source.casefold() == _select_option_name(local.source, "General").casefold():
        keep["source"] = local.source
    pushed_tags = [n for n in (_select_option_name(t, "") for t in local.tags) if n]
    remote_tags = [t.casefold() for t in remote.tags]
    if remote_tags in (
        [t.casefold() for t in pushed_tags],
        [t.casefold() for t in pushed_tags + ["severity:Medium"]],
    ):
        keep["tags"] = local.tags
    # Some older pages carry provenance in a Solution footer, with no Project.
    footer = f"\n\nSource: {local.source}\nRecall ID: {local.si_id}"
    if remote.fix.endswith(footer):
        remote = remote.model_copy(update={"fix": remote.fix.removesuffix(footer).rstrip()})
        if remote.source == "General":
            keep["source"] = local.source
    body = ("symptoms", "root_cause", "fix")
    pushed_body = parse_solution(_notion_solution(local))
    if all(getattr(remote, k) == pushed_body[k] for k in body):
        keep.update({k: getattr(local, k) for k in body})
    return remote.model_copy(update=keep)


def sync_to_notion(db: "RecallDB", client: NotionClient) -> int:
    """Push local-first issues that have no Notion page yet.

    This is intentionally cadence-driven: agent requests write only to the
    local SQLite index. A failed page create leaves the row unsynced so the
    next cadence retries it without losing the local entry.
    """
    pushed = 0
    for issue in db.unsynced_issues():
        try:
            page_id, edited_at = client.create_page(
                build_notion_properties(
                    _notion_title(issue), _notion_solution(issue), issue.source, issue.tags
                )
            )
            # Stamping the edit time stops the next pull from re-reading this page.
            db.link_notion_page(issue.si_id, page_id, edited_at)
            pushed += 1
        except Exception as exc:
            log.warning("notion sync: push failed for %s, will retry: %s", issue.si_id, exc)
    return pushed


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
        local = db.get_issue_by_notion_page(page_id)
        if local is not None:
            issue.si_id = derive_si_id(page_id, issue.title, local.si_id)
            issue = _merge_with_local(issue, local)
        issue.embedding = engine.embed(f"{issue.title} {issue.symptoms} {issue.root_cause}")
        db.insert_issue(issue)
        changed += 1
    log.info("notion sync: %d page(s) upserted, %d total in index", changed, db.count())
    return changed
