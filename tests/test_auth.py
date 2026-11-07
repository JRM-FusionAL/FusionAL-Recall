"""Loopback default, compose port binding and optional bearer auth for write tools."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from recall.embeddings import EmbeddingEngine

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-write-token-0123456789"


def _blob() -> bytes:
    return struct.pack("384f", *([0.05] * 384))


@pytest.fixture()
def srv(db, monkeypatch):
    """recall.server wired to an in-memory DB and a fake embedding engine."""
    import recall.server as module

    engine = MagicMock(spec=EmbeddingEngine)
    engine.embed.return_value = _blob()
    monkeypatch.setattr(module, "_db", db)
    monkeypatch.setattr(module, "_engine", engine)
    monkeypatch.setattr(module, "NOTION_TOKEN", "")
    monkeypatch.setattr(module, "_notion", None)
    monkeypatch.delenv("RECALL_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("RECALL_AUTH_TOKEN_FILE", raising=False)
    return module


def _ctx(headers: dict[str, str] | None = None) -> SimpleNamespace:
    """Mimic mcp's Context for a streamable-HTTP tool call with these headers."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return SimpleNamespace(request_context=SimpleNamespace(request=Request(scope)))


def _remember(srv, ctx=None):
    return srv.remember(
        title="Auth test",
        symptoms="s",
        root_cause="r",
        fix="f",
        ctx=ctx,
    )


# ---------------------------------------------------------------------------
# Defaults and deployment files
# ---------------------------------------------------------------------------

def test_default_host_is_loopback():
    env = {k: v for k, v in os.environ.items() if k != "RECALL_HOST"}
    result = subprocess.run(
        [sys.executable, "-c", "import recall.server as s; print(s.HOST)"],
        cwd=str(REPO_ROOT),
        env={**env, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "127.0.0.1"


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.yaml"])
def test_compose_publishes_port_on_loopback_only(compose_file):
    text = (REPO_ROOT / compose_file).read_text(encoding="utf-8")
    publish_lines = [line for line in text.splitlines() if re.search(r'"[^"]*:8107"', line)]
    assert publish_lines, f"no 8107 publish found in {compose_file}"
    for line in publish_lines:
        assert '"127.0.0.1:' in line, line


# ---------------------------------------------------------------------------
# Write auth on remember()
# ---------------------------------------------------------------------------

def test_remember_open_when_no_token_configured(srv, db):
    result = _remember(srv, _ctx())
    assert result["si_id"] == "SI-001"
    assert db.count() == 1


def test_in_process_call_is_not_checked_when_token_set(srv, db, monkeypatch):
    monkeypatch.setenv("RECALL_AUTH_TOKEN", TOKEN)
    result = _remember(srv, ctx=None)
    assert result["si_id"] == "SI-001"


def test_remember_rejects_request_without_token(srv, db, monkeypatch):
    monkeypatch.setenv("RECALL_AUTH_TOKEN", TOKEN)
    with pytest.raises(PermissionError):
        _remember(srv, _ctx())
    assert db.count() == 0


@pytest.mark.parametrize(
    "header",
    [
        f"Bearer wrong-token",
        f"Basic {TOKEN}",
        f"Bearer ",
        TOKEN,
    ],
)
def test_remember_rejects_wrong_or_malformed_credentials(srv, db, monkeypatch, header):
    monkeypatch.setenv("RECALL_AUTH_TOKEN", TOKEN)
    with pytest.raises(PermissionError):
        _remember(srv, _ctx({"Authorization": header}))
    assert db.count() == 0


def test_remember_accepts_correct_bearer_token(srv, db, monkeypatch):
    monkeypatch.setenv("RECALL_AUTH_TOKEN", TOKEN)
    result = _remember(srv, _ctx({"Authorization": f"Bearer {TOKEN}"}))
    assert result["si_id"] == "SI-001"
    assert db.count() == 1


def test_token_file_is_used_and_overrides_env(srv, db, monkeypatch, tmp_path):
    token_file = tmp_path / "recall-token"
    token_file.write_text(f"{TOKEN}\n", encoding="utf-8")
    os.chmod(token_file, 0o600)
    monkeypatch.setenv("RECALL_AUTH_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("RECALL_AUTH_TOKEN", "env-token-that-is-ignored")
    with pytest.raises(PermissionError):
        _remember(srv, _ctx({"Authorization": "Bearer env-token-that-is-ignored"}))
    result = _remember(srv, _ctx({"Authorization": f"Bearer {TOKEN}"}))
    assert result["si_id"] == "SI-001"


def test_empty_token_file_means_auth_not_configured(srv, db, monkeypatch, tmp_path):
    token_file = tmp_path / "empty-token"
    token_file.write_text("\n", encoding="utf-8")
    os.chmod(token_file, 0o600)
    monkeypatch.setenv("RECALL_AUTH_TOKEN_FILE", str(token_file))
    assert srv._configured_write_token() is None
    assert _remember(srv, _ctx())["si_id"] == "SI-001"


def test_unreadable_token_file_fails_closed(srv, db, monkeypatch, tmp_path):
    monkeypatch.setenv("RECALL_AUTH_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    with pytest.raises(RuntimeError, match="unreadable"):
        _remember(srv, _ctx())
    assert db.count() == 0


def test_group_readable_token_file_logs_warning(srv, monkeypatch, tmp_path, caplog):
    token_file = tmp_path / "loose-token"
    token_file.write_text(TOKEN, encoding="utf-8")
    os.chmod(token_file, 0o644)
    monkeypatch.setenv("RECALL_AUTH_TOKEN_FILE", str(token_file))
    with caplog.at_level(logging.WARNING, logger="recall.server"):
        assert srv._configured_write_token() == TOKEN
    assert any("chmod 600" in rec.getMessage() for rec in caplog.records)


def test_unauthenticated_startup_logs_warning(srv, caplog):
    with caplog.at_level(logging.WARNING, logger="recall.server"):
        srv._warn_if_writes_unauthenticated()
    assert any("accepts unauthenticated writes" in rec.getMessage() for rec in caplog.records)


def test_configured_token_suppresses_startup_warning(srv, monkeypatch, caplog):
    monkeypatch.setenv("RECALL_AUTH_TOKEN", TOKEN)
    with caplog.at_level(logging.WARNING, logger="recall.server"):
        srv._warn_if_writes_unauthenticated()
    assert not caplog.records


def test_read_tools_are_not_gated_by_write_token(srv, db, monkeypatch):
    from recall.models import Issue
    from datetime import datetime, timezone

    db.insert_issue(
        Issue(
            si_id="SI-001",
            title="Existing",
            symptoms="s",
            root_cause="r",
            fix="f",
            created_at=datetime.now(timezone.utc),
            embedding=_blob(),
        )
    )
    monkeypatch.setenv("RECALL_AUTH_TOKEN", TOKEN)
    assert srv.verify("SI-001")["title"] == "Existing"
    assert srv.list_recent(n=5)[0]["si_id"] == "SI-001"


def test_context_parameter_is_not_exposed_as_tool_input(srv):
    tools = asyncio.run(srv.app.list_tools())
    remember_tool = next(t for t in tools if t.name == "remember")
    properties = remember_tool.inputSchema.get("properties", {})
    assert "ctx" not in properties
    assert "title" in properties
