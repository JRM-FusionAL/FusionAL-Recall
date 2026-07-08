"""Pydantic v2 models for FusionAL Recall."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


class Issue(BaseModel):
    """A solved issue entry in the recall registry."""

    si_id: str = Field(..., description="Unique identifier, e.g. SI-001")
    title: str = Field(..., description="One-line summary of the issue")
    symptoms: str = Field(..., description="What the agent observed")
    root_cause: str = Field(..., description="What was actually wrong")
    fix: str = Field(..., description="Exact steps that resolved the issue")
    source: str = Field(default="", description="Session or repo context")
    tags: List[str] = Field(default_factory=list, description="Searchable labels")
    verified_at: Optional[str] = Field(None, description="YYYY-MM verification date")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tier: str = Field(default="personal", description="personal | project | public")
    embedding: Optional[bytes] = Field(None, exclude=True, description="float32 vector blob")
    notion_page_id: Optional[str] = Field(None, description="Linked Notion page ID")
    notion_edited_at: Optional[str] = Field(None, description="Notion last_edited_time at last sync")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "si_id": "SI-001",
                    "title": "Claude Desktop server timeout above 8 servers",
                    "symptoms": "Claude Desktop hangs on startup when >8 MCP servers configured",
                    "root_cause": "Desktop client enforces a hard timeout on MCP server init",
                    "fix": "Consolidate via FusionAL gateway — expose one MCP endpoint, proxy internally",
                    "source": "2026-01 setup session",
                    "tags": ["claude-desktop", "mcp", "timeout", "fusional"],
                    "verified_at": "2026-01",
                    "tier": "personal",
                }
            ]
        }
    }


class QueryResult(BaseModel):
    """A search result with similarity score."""

    si_id: str
    title: str
    symptoms: str
    root_cause: str
    fix: str
    source: str
    tags: List[str]
    similarity: float = Field(..., ge=0.0, le=1.0)
    tier: str


class RememberResult(BaseModel):
    """Confirmation that a new SI entry was stored."""

    si_id: str
    title: str
    created_at: datetime
    tier: str
    message: str
    notion_synced: bool = False
