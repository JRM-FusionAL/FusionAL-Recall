#!/usr/bin/env python3
"""Regenerate 05-RECALL/SOLVED-ISSUES.md from the FusionAL-Recall SQLite index.

SOLVED-ISSUES.md is a MIRROR, not a source of truth. The recall service
(localhost:8107, `remember` MCP tool) writes to recall.db and mirrors to Notion;
nothing ever wrote back to the markdown file, so it drifted 53+ entries behind.

This script is idempotent and safe to run on a timer:

    python export_solved_issues.py                  # dry-run: report, write nothing
    python export_solved_issues.py --write          # write the file (keeps .bak)

SI entries already in the file are NOT updated from the database: the file's
block wins. A database correction therefore does not reach the file, a hand
edit does not reach the database, and one SI number can name two different
issues. Each run reports these conflicts but does not resolve them, because
either side may hold the only good copy.

What it preserves: the preamble (everything before the first `## SI-###:` entry)
and every human-authored section whose heading is NOT an SI entry (TEMPLATE
blocks, narrative follow-through sections). Those are re-emitted after the
entries, in their original order.

What it generates: SI-### entries that are not yet in the file, in numeric
order, and every non-numeric entry (N-<hex> Notion-only pages, PI-###).

Exit codes: 0 = in sync (or wrote cleanly), 1 = drift found in dry-run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

RECALL_DB = Path.home() / "Projects/FusionAL-Recall/recall.db"
TARGET = Path.home() / "Projects/fusional-knowledge-base/05-RECALL/SOLVED-ISSUES.md"
HEADER = "# FusionAL Recall — Solved Issues Registry"
SI_HEAD = re.compile(r"^## (SI-\d+):(.*)$", re.M)
ANY_ENTRY_HEAD = re.compile(r"^## (SI-\d+|N-[0-9a-f]+|PI-\d+):", re.M)
SI_HEAD_ANY = re.compile(r"^## SI-", re.M)  # first SI entry, numeric or not


def _tags(raw) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("["):
        try:
            return ", ".join(str(t) for t in json.loads(raw))
        except Exception:
            pass
    return raw


def render(issue: sqlite3.Row, show_id: str | None = None) -> str:
    sid = show_id or issue["si_id"]
    title = re.sub(r"^%s\s*[—–:-]\s*" % re.escape(issue["si_id"]), "", issue["title"] or "")
    verified = (issue["created_at"] or "")[:10]
    return (
        f"## {sid}: {title}\n"
        f"**Symptoms:** {issue['symptoms'] or ''}\n"
        f"**Root cause:** {issue['root_cause'] or ''}\n"
        f"**Fix:** {issue['fix'] or ''}\n"
        f"**Verified:** {verified} | **Source:** {issue['source'] or 'not recorded'}\n"
        f"**Tags:** {_tags(issue['tags'])}\n"
    )


def split_existing(text: str):
    """Return (preamble, {SI-id: block}, [human sections]) from the current file."""
    first = SI_HEAD_ANY.search(text)
    preamble = text[: first.start()] if first else text
    heads = [m for m in re.finditer(r"^## .*$", text, re.M)]
    sections = []
    file_entries: dict[str, str] = {}
    for i, m in enumerate(heads):
        line = m.group(0)
        # anything before the first entry is preamble; never re-emit it as a section
        if first and m.start() < first.start():
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        block = text[m.start():end].rstrip() + "\n"
        em = re.match(r"^## (SI-\d+):", line)
        if em:
            file_entries[em.group(1)] = block
            continue
        if re.match(r"^## (N-[0-9a-f]+|PI-\d+):", line):
            continue
        if line.startswith("## NOTION-ONLY ENTRIES"):
            continue  # generated section, not human-authored
        sections.append(block)
    # de-dupe identical TEMPLATE blocks, keep order
    seen, out = set(), []
    for s in sections:
        key = s.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return preamble.rstrip() + "\n", file_entries, out


def _norm_block(block: str) -> str:
    """Block text without spacing or trailing rule differences."""
    return re.sub(r"\s+", " ", block).strip().rstrip("- ").strip()


def _title_key(block: str) -> str:
    title = block.split("\n", 1)[0].split(":", 1)[-1]
    return re.sub(r"[^a-z0-9]", "", title.lower())


def find_conflicts(file_entries: dict[str, str], db_entries: dict[str, str]):
    """Compare shared SI ids. Return (differing ids, ids whose titles differ a lot)."""
    differing, titles = [], []
    for sid in sorted(file_entries.keys() & db_entries.keys(), key=lambda s: int(s[3:])):
        ours, theirs = file_entries[sid], db_entries[sid]
        if _norm_block(ours) == _norm_block(theirs):
            continue
        differing.append(sid)
        ratio = difflib.SequenceMatcher(
            None, _title_key(ours), _title_key(theirs), autojunk=False
        ).ratio()
        if ratio < 0.6:
            titles.append(sid)
    return differing, titles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write the file (default: dry-run)")
    ap.add_argument("--db", type=Path, default=RECALL_DB)
    ap.add_argument("--target", type=Path, default=TARGET)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"FAIL: recall db not found: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM issues").fetchall()

    numeric, notable = [], []
    for r in rows:
        m = re.fullmatch(r"SI-(\d+)", r["si_id"])
        if m:
            n = int(m.group(1))
            # the file has always used zero-padded ids (SI-001); the db stores SI-1
            numeric.append((n, render(r, show_id=f"SI-{n:03d}")))
        else:
            notable.append(r)
    numeric.sort(key=lambda x: x[0])
    notable.sort(key=lambda r: (r["si_id"], r["created_at"] or ""))

    text = args.target.read_text() if args.target.exists() else ""
    preamble, file_entries, human = split_existing(text)
    have = set(file_entries)

    parts = [preamble, ""]
    for n, block in numeric:
        # a file-only entry (never migrated to the db) must never be destroyed
        parts.append(file_entries.get(f"SI-{n:03d}", block))
    file_only = sorted(set(file_entries) - {f"SI-{n:03d}" for n, _ in numeric},
                       key=lambda s: int(s[3:]))
    for sid in file_only:
        parts.append(file_entries[sid])
    if notable:
        parts.append(
            "## NOTION-ONLY ENTRIES — no numeric SI id\n\n"
            "Pulled from the Notion mirror. These pages exist in Notion but were\n"
            "never assigned a sequential SI number (the historical drift). Logged\n"
            "via the `:8107` `remember` tool they acquire an SI id; these are listed\n"
            "here for completeness so the file is never blind to them.\n"
        )
        for r in notable:
            parts.append(render(r))
    parts.extend(human)
    new_text = "\n".join(p.rstrip() + "\n" for p in parts if p is not None)
    # collapse 3+ blank lines
    new_text = re.sub(r"\n{3,}", "\n\n", new_text).rstrip() + "\n"

    new_ids = {f"SI-{n:03d}" for n, _ in numeric}
    added = sorted(new_ids - have, key=lambda s: int(s[3:]))
    removed = sorted(have - new_ids - set(file_only), key=lambda s: int(s[3:]))

    print(f"recall rows: {len(rows)} | numeric SI: {len(numeric)} | non-numeric: {len(notable)}")
    print(f"target: {args.target} ({len(text)} chars -> {len(new_text)} chars)")
    print(f"entries added: {len(added)}{(' ' + ', '.join(added[:12])) if added else ''}")
    if removed:
        print(f"entries removed: {len(removed)} {' '.join(removed[:12])}")
    if file_only:
        print(f"FILE-ONLY entries preserved (never migrated to recall db: "
              f"log them via :8107 remember): {' '.join(file_only)}")
    print(f"non-numeric entries listed: {len(notable)}")
    differing, titles = find_conflicts(file_entries, {f"SI-{n:03d}": b for n, b in numeric})
    if differing:
        print(f"CONFLICT: {len(differing)} SI entries differ between file and db "
              f"(file version kept): {' '.join(differing)}")
    if titles:
        print(f"CONFLICT: {len(titles)} of those have very different titles, possibly "
              f"two issues under one id: {' '.join(titles)}")

    if new_text == text:
        print("IN SYNC — nothing to write")
        return 0
    if not args.write:
        print("DRIFT — rerun with --write to update")
        return 1

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(args.target, args.target.with_suffix(f".md.bak-{stamp}"))
    args.target.write_text(new_text)
    print(f"WROTE {args.target} (backup .md.bak-{stamp})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
