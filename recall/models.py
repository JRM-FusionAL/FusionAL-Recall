from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class Issue(BaseModel):
    """Solved issue entry from the registry."""
    si_id: str = Field(..., description="Issue ID (e.g., SI-001)")
    title: str = Field(..., description="One-line title")
    symptoms: str = Field(..., description="What the problem looked like")
    root_cause: str = Field(..., description="What was actually wrong")
    fix: str = Field(..., description="Exact steps that worked")
    source: str = Field(..., description="Session/repo/article context")
    tags: List[str] = Field(default_factory=list, description="Comma-separated tags")
    verified_at: Optional[str] = Field(None, description="Verification date (YYYY-MM)")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    tier: str = Field(default="personal", description="Access tier: personal, project, or public")
    embedding: Optional[bytes] = Field(None, description="float32 embedding bytes from sqlite-vec")

    model_config = {
        "json_schema_extra": {
            "example": {
                "si_id": "SI-001",
                "title": "Claude Desktop server timeout above 8 servers",
                "symptoms": "MCP servers fail to load, timeout errors in Claude Desktop logs",
                "root_cause": "Claude Desktop has an effective ~8 server limit before init timeouts",
                "fix": "Consolidate via FusionAL gateway (single MCP entry, N tools behind it)",
                "source": "FusionAL config debugging session",
                "tags": ["claude-desktop", "mcp", "timeout", "windows"],
                "verified_at": "2026-03",
                "tier": "public"
            }
        }
    }


class QueryResult(BaseModel):
    """Search result from semantic query."""
    si_id: str
    title: str
    symptoms: str
    root_cause: str
    fix: str
    source: str
    tags: List[str]
    similarity: float = Field(..., description="Similarity score 0.0-1.0")
    tier: str


class RememberResult(BaseModel):
    """Response from remember() tool."""
    si_id: str = Field(..., description="Auto-assigned SI ID")
    title: str
    created_at: datetime
    tier: str
    message: str = Field(default="Entry created successfully")
