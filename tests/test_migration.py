import textwrap
import tempfile
import os
import pytest
from recall.migrate import parse_solved_issues


SAMPLE_SOLVED_ISSUES = textwrap.dedent("""\
    # FusionAL Recall — Solved Issues Registry

    ---

    ## SI-001: Claude Desktop server timeout above 8 servers
    **Symptoms:** MCP servers fail to load, timeout errors in Claude Desktop logs
    **Root cause:** Claude Desktop has an effective ~8 server limit before init timeouts
    **Fix:** Consolidate via FusionAL gateway (single MCP entry, N tools behind it)
    **Verified:** 2026-03 | **Source:** FusionAL config debugging session
    **Tags:** claude-desktop, mcp, timeout, windows

    ---

    ## SI-002: localhost MCP URLs unreachable from remote Claude Desktop
    **Symptoms:** Servers work locally on T3610, fail when Claude Desktop runs on laptop
    **Root cause:** localhost in MCP config resolves to the client machine, not the server
    **Fix:** Replace localhost with Tailscale IP in claude_desktop_config.json
    **Verified:** 2026-03 | **Source:** FusionAL remote access setup
    **Tags:** tailscale, mcp, networking

    ## TEMPLATE — copy this for new entries
""")


@pytest.fixture
def solved_issues_file(tmp_path):
    f = tmp_path / "SOLVED-ISSUES.md"
    f.write_text(SAMPLE_SOLVED_ISSUES, encoding="utf-8")
    return str(f)


def test_parse_count(solved_issues_file):
    issues = parse_solved_issues(solved_issues_file)
    assert len(issues) == 2


def test_parse_si001_fields(solved_issues_file):
    issues = parse_solved_issues(solved_issues_file)
    si001 = next(i for i in issues if i.si_id == "SI-001")
    assert si001.title == "Claude Desktop server timeout above 8 servers"
    assert "MCP servers fail to load" in si001.symptoms
    assert "~8 server limit" in si001.root_cause
    assert "FusionAL gateway" in si001.fix
    assert si001.verified_at == "2026-03"
    assert si001.source == "FusionAL config debugging session"
    assert "claude-desktop" in si001.tags
    assert "timeout" in si001.tags
    assert si001.tier == "personal"


def test_parse_si002_tags(solved_issues_file):
    issues = parse_solved_issues(solved_issues_file)
    si002 = next(i for i in issues if i.si_id == "SI-002")
    assert si002.tags == ["tailscale", "mcp", "networking"]


def test_parse_real_file():
    """Integration test: parse the actual knowledge-base file if present."""
    kb_path = os.path.join(
        os.path.expanduser("~"),
        "Projects",
        "fusional-knowledge-base",
        "05-RECALL",
        "SOLVED-ISSUES.md",
    )
    if not os.path.exists(kb_path):
        pytest.skip("Knowledge base not present on this machine")
    issues = parse_solved_issues(kb_path)
    assert len(issues) >= 1
    si_ids = [i.si_id for i in issues]
    assert "SI-001" in si_ids
