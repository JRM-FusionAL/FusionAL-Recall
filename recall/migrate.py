"""Parse SOLVED-ISSUES.md and load entries into the recall database."""

import re
from pathlib import Path
from typing import List
from .models import Issue


_BLOCK_RE = re.compile(
    r"^## (SI-\d+): (.+?)\n"
    r"\*\*Symptoms:\*\* (.+?)\n"
    r"\*\*Root cause:\*\* (.+?)\n"
    r"\*\*Fix:\*\* (.+?)\n"
    r"\*\*Verified:\*\* (.+?) \| \*\*Source:\*\* (.+?)\n"
    r"\*\*Tags:\*\* (.+?)(?:\n|$)",
    re.MULTILINE | re.DOTALL,
)


def parse_solved_issues(path: str | Path) -> List[Issue]:
    """Parse a SOLVED-ISSUES.md file and return a list of Issue objects."""
    text = Path(path).read_text(encoding="utf-8")
    issues: List[Issue] = []

    for m in _BLOCK_RE.finditer(text):
        si_id, title, symptoms, root_cause, fix, verified_at, source, tags_raw = (
            m.group(1),
            m.group(2).strip(),
            m.group(3).strip(),
            m.group(4).strip(),
            m.group(5).strip(),
            m.group(6).strip(),
            m.group(7).strip(),
            m.group(8).strip(),
        )
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        issues.append(
            Issue(
                si_id=si_id,
                title=title,
                symptoms=symptoms,
                root_cause=root_cause,
                fix=fix,
                source=source,
                tags=tags,
                verified_at=verified_at,
                tier="personal",
            )
        )

    return issues
