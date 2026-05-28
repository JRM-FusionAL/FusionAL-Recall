import sqlite3
import struct
from typing import List, Optional
from datetime import datetime
from .models import Issue, QueryResult


class RecallDB:
    """SQLite database manager with sqlite-vec for semantic search."""

    def __init__(self, db_path: str = ":memory:"):
        """Initialize database connection and schema.
        
        Args:
            db_path: Path to SQLite file (default: in-memory for testing)
        """
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        """Create tables and enable sqlite-vec extension."""
        cursor = self.conn.cursor()
        
        # Load sqlite-vec extension if available; fall back to Python-side cosine search
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
        except (ImportError, AttributeError, sqlite3.OperationalError):
            pass
        
        # Create issues table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS issues (
                si_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                symptoms TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                fix TEXT NOT NULL,
                source TEXT NOT NULL,
                tags TEXT,
                verified_at TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tier TEXT DEFAULT 'personal',
                embedding BLOB
            )
        """)
        
        # Create index on tier for filtering
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_issues_tier ON issues(tier)
        """)
        
        self.conn.commit()

    def insert_issue(self, issue: Issue) -> None:
        """Insert an issue into the database.
        
        Args:
            issue: Issue object to insert
        """
        cursor = self.conn.cursor()
        tags_str = ",".join(issue.tags) if issue.tags else ""
        
        cursor.execute("""
            INSERT OR REPLACE INTO issues 
            (si_id, title, symptoms, root_cause, fix, source, tags, verified_at, created_at, tier, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            issue.si_id,
            issue.title,
            issue.symptoms,
            issue.root_cause,
            issue.fix,
            issue.source,
            tags_str,
            issue.verified_at,
            issue.created_at,
            issue.tier,
            issue.embedding
        ))
        self.conn.commit()

    def search_by_embedding(self, embedding_bytes: bytes, limit: int = 5, tier: Optional[str] = None) -> List[QueryResult]:
        """Search for similar issues by embedding vector.
        
        Args:
            embedding_bytes: Query embedding as bytes
            limit: Maximum results to return
            tier: Filter by access tier (optional)
            
        Returns:
            List of QueryResult objects ranked by similarity
        """
        cursor = self.conn.cursor()
        
        # Convert bytes back to float list for comparison
        embedding = struct.unpack(f'{len(embedding_bytes)//4}f', embedding_bytes)
        
        # Simple cosine similarity via dot product (placeholder; real implementation would use sqlite-vec)
        query_sql = """
            SELECT si_id, title, symptoms, root_cause, fix, source, tags, tier, embedding
            FROM issues
        """
        
        params = []
        if tier:
            query_sql += " WHERE tier = ?"
            params.append(tier)
        query_sql += " LIMIT ?"
        params.append(limit)

        cursor.execute(query_sql, params)
        results = []
        
        for row in cursor.fetchall():
            # Compute basic similarity (in production, use sqlite-vec's native vector search)
            similarity = 0.5  # Placeholder
            if row['embedding']:
                stored_embedding = struct.unpack(f'{len(row["embedding"])//4}f', row['embedding'])
                similarity = self._cosine_similarity(embedding, stored_embedding)
            
            tags = row['tags'].split(",") if row['tags'] else []
            result = QueryResult(
                si_id=row['si_id'],
                title=row['title'],
                symptoms=row['symptoms'],
                root_cause=row['root_cause'],
                fix=row['fix'],
                source=row['source'],
                tags=tags,
                similarity=similarity,
                tier=row['tier']
            )
            results.append(result)
        
        return sorted(results, key=lambda x: x.similarity, reverse=True)

    def get_issue_by_id(self, si_id: str) -> Optional[Issue]:
        """Retrieve a single issue by ID.
        
        Args:
            si_id: Issue ID (e.g., SI-001)
            
        Returns:
            Issue object or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM issues WHERE si_id = ?", (si_id,))
        row = cursor.fetchone()
        
        if not row:
            return None
        
        tags = row['tags'].split(",") if row['tags'] else []
        return Issue(
            si_id=row['si_id'],
            title=row['title'],
            symptoms=row['symptoms'],
            root_cause=row['root_cause'],
            fix=row['fix'],
            source=row['source'],
            tags=tags,
            verified_at=row['verified_at'],
            created_at=datetime.fromisoformat(row['created_at']),
            tier=row['tier'],
            embedding=row['embedding']
        )

    def list_recent_issues(self, n: int = 10, tier: Optional[str] = None) -> List[Issue]:
        """List N most recently created issues.
        
        Args:
            n: Number of issues to return
            tier: Filter by access tier (optional)
            
        Returns:
            List of Issue objects ordered by creation date (newest first)
        """
        cursor = self.conn.cursor()
        
        query = "SELECT * FROM issues"
        params = []
        
        if tier:
            query += " WHERE tier = ?"
            params.append(tier)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(n)
        
        cursor.execute(query, params)
        issues = []
        
        for row in cursor.fetchall():
            tags = row['tags'].split(",") if row['tags'] else []
            issue = Issue(
                si_id=row['si_id'],
                title=row['title'],
                symptoms=row['symptoms'],
                root_cause=row['root_cause'],
                fix=row['fix'],
                source=row['source'],
                tags=tags,
                verified_at=row['verified_at'],
                created_at=datetime.fromisoformat(row['created_at']),
                tier=row['tier'],
                embedding=row['embedding']
            )
            issues.append(issue)
        
        return issues

    def count(self) -> int:
        """Return total number of issues in the database."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM issues")
        return cursor.fetchone()[0]

    def get_next_si_id(self) -> str:
        """Return the next available SI-XXX identifier."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT si_id FROM issues ORDER BY si_id DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            return "SI-001"
        last_num = int(row[0].split("-")[1])
        return f"SI-{last_num + 1:03d}"

    def close(self):
        """Close the database connection."""
        self.conn.close()

    @staticmethod
    def _cosine_similarity(a: tuple, b: tuple) -> float:
        """Compute cosine similarity between two float vectors."""
        if len(a) != len(b):
            return 0.0
        
        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        
        if norm_a == 0 or norm_b == 0:
            return 0.0
        
        return dot_product / (norm_a * norm_b)
