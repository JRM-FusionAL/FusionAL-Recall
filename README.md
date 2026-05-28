# FusionAL Recall — Semantic Registry Search MCP

Recall wraps the [FusionAL Solved-Issues Registry](https://github.com/JRM-FusionAL/fusional-knowledge-base/blob/awesomeness/05-RECALL/SOLVED-ISSUES.md) with semantic search, three-tier access control, and auto-migration.

Query your team's solved problems via MCP tools: `recall()` for semantic search, `remember()` to log new entries, `list_recent()` for recent issues, `verify()` to validate entry integrity.

## Features

- **Semantic Search**: Query by problem description, not keyword. "claude desktop timeout" returns SI-001 even if you don't say "timeout" or "claude" exactly.
- **Automatic Migration**: Parse existing SOLVED-ISSUES.md markdown on first run; auto-assign SI-XXX IDs; populate SQLite.
- **Three-Tier Access**: `personal` (agent-only), `project` (team-scoped), `public` (shared community).
- **Vector Storage**: sentence-transformers + sqlite-vec for fast CPU-friendly embeddings.
- **MCP Tools**: `recall(query, tier, limit)`, `remember(symptoms, root_cause, fix, source, tags)`, `list_recent(n)`, `verify(si_id)`, `get(si_id)`.

## Architecture

```
SOLVED-ISSUES.md (markdown source)
         ↓
   [Migrate on startup]
         ↓
SQLite + sqlite-vec (embeddings index)
         ↓
FastMCP Server (port 8107)
         ↓
      [Tools]
```

## SQL Schema

```sql
CREATE TABLE issues (
  si_id TEXT PRIMARY KEY,           -- SI-001, SI-002, etc.
  title TEXT NOT NULL,               -- one-line summary
  symptoms TEXT NOT NULL,
  root_cause TEXT NOT NULL,
  fix TEXT NOT NULL,
  source TEXT,                       -- session/task context
  tags TEXT,                         -- comma-separated
  verified_at TEXT,                  -- YYYY-MM
  created_at TEXT NOT NULL,          -- ISO 8601
  tier TEXT DEFAULT 'personal',      -- personal, project, public
  embedding BLOB NOT NULL            -- float32 vec (float-serialized)
);
```

## Usage

### Start the Server

```bash
python -m recall.server
# Listens on 0.0.0.0:8107
```

On first run, the server:
1. Reads SOLVED-ISSUES.md (path from SOLVED_ISSUES_PATH env var)
2. Parses SI-XXX markdown blocks
3. Generates embeddings for symptoms + root_cause + fix (combined text)
4. Inserts into SQLite + sqlite-vec index
5. Auto-assigns next SI-ID based on highest existing ID

### MCP Tools

#### `recall(query: str, tier: str = "personal", limit: int = 5) → List[Issue]`

Semantic search. Returns issues matching the query, ranked by embedding similarity.

```python
# Query: "claude desktop timeout"
# Returns: SI-001 (Claude Desktop server timeout above 8 servers)
results = await client.call_tool(
    name="recall",
    arguments={"query": "claude desktop timeout", "tier": "personal", "limit": 5}
)
```

#### `remember(symptoms: str, root_cause: str, fix: str, source: str, tags: str) → Dict`

Log a new issue. Returns the assigned SI-ID (e.g., SI-012).

```python
result = await client.call_tool(
    name="remember",
    arguments={
        "symptoms": "My symptoms here",
        "root_cause": "Root cause",
        "fix": "Steps to fix",
        "source": "session context",
        "tags": "tag1,tag2"
    }
)
# Returns: {"si_id": "SI-012", "title": "...", "created_at": "..."}
```

#### `list_recent(n: int = 10) → List[Issue]`

Return the N most recently added issues.

```python
recent = await client.call_tool(
    name="list_recent",
    arguments={"n": 10}
)
```

#### `verify(si_id: str) → Dict`

Check if an SI entry exists and is valid.

```python
valid = await client.call_tool(
    name="verify",
    arguments={"si_id": "SI-001"}
)
```

#### `get(si_id: str) → Dict`

Retrieve full entry by SI-ID.

```python
entry = await client.call_tool(
    name="get",
    arguments={"si_id": "SI-001"}
)
```

## Deployment

### Docker

```bash
docker build -t fusional-recall:latest .
docker run -p 8107:8107 \
  -e SOLVED_ISSUES_PATH=/mnt/kb/05-RECALL/SOLVED-ISSUES.md \
  -v /path/to/kb:/mnt/kb \
  fusional-recall:latest
```

### Docker Compose

```bash
docker-compose up -d
# Recalls listens on localhost:8107
# recall.db volume persists embeddings across restarts
```

## Testing

```bash
pytest tests/ -v
```

Tests validate:
- Migration: SOLVED-ISSUES.md parses correctly; all entries load into SQLite
- Recall: Semantic search returns SI-001 for query "claude desktop timeout"
- Remember: New entry is assigned SI-012, written to DB, queryable immediately
- Verify: SI-001 exists and validates; SI-999 does not
- Tier filtering: personal/project/public tiers are enforced

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `RECALL_HOST` | `0.0.0.0` | MCP server bind address |
| `RECALL_PORT` | `8107` | MCP server port |
| `RECALL_DB_PATH` | `./recall.db` | SQLite database file |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model |
| `DEFAULT_TIER` | `personal` | Default access tier |
| `SOLVED_ISSUES_PATH` | `../fusional-knowledge-base/05-RECALL/SOLVED-ISSUES.md` | Path to registry markdown |

## License

MIT. See LICENSE.
