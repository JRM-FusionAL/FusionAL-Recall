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


def derive_si_id(page_id: str, title: str, solution: str) -> str:
    """Reuse a legacy SI-ID if the entry references one; else derive from page ID."""
    m = _SI_ID.search(title) or _SI_ID.search(solution)
    if m:
        return m.group(0)
    return "N-" + page_id.replace("-", "")[:8]


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
        si_id=derive_si_id(page["id"], title, solution),
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
