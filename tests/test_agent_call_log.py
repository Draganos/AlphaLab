"""Agent Research Interface, PR B: the append-only call log and the
transport-independent service (no MCP library needed)."""

import ast
import hashlib
import importlib.util
import logging
import sqlite3
import sys
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from alpha_lab.agent_interface import AgentContext, call_tool, make_readonly_engine
from alpha_lab.agent_interface.call_log import (
    MAX_LOGGED_ARGUMENT_CHARS,
    CallLog,
    canonical_json,
)
from alpha_lab.agent_interface.service import AgentService
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "alpha_lab" / "agent_interface"
TODAY = date(2026, 9, 20)


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'research.db'}"
    engine = make_engine(url)
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="TST", company_name="Test Co", asset_type="EQUITY", is_tracked=True))
        session.add(Price(ticker="TST", date=TODAY, close=1, adjusted_close=1, provider="test",
                          ingested_at=datetime(2026, 9, 19, tzinfo=UTC)))
        session.commit()
    engine.dispose()
    return url


@pytest.fixture
def log(tmp_path):
    entry = CallLog(tmp_path / "calls.db")
    yield entry
    entry.close()


@pytest.fixture
def service(db_url, log):
    engine = make_readonly_engine(db_url)
    yield AgentService(AgentContext(engine, load_settings(), lambda: TODAY), log)
    engine.dispose()


def _envelope(**overrides):
    base = {"interface_version": "agent-interface-v1", "tool": "get_methodology", "ok": True,
            "system_date": "2026-09-20", "ticker": "TST", "requested_as_of": None,
            "provenance_ids": ["b", "a"], "data": {"x": 1}, "error": None}
    return {**base, **overrides}


def test_a_call_is_recorded_with_the_exact_response_its_hash_and_provenance(log):
    envelope = _envelope()
    row_id = log.record(tool="get_methodology", arguments={"ticker": "TST"}, envelope=envelope,
                        duration_ms=1.23456, client="claude-desktop",
                        now=datetime(2026, 9, 20, 12, tzinfo=UTC))
    [row] = log.entries()
    assert row["id"] == row_id == 1
    assert row["called_at"] == "2026-09-20T12:00:00+00:00" and row["client"] == "claude-desktop"
    assert row["tool"] == "get_methodology" and row["arguments"] == '{"ticker":"TST"}'
    assert row["ok"] == 1 and row["error_code"] is None and row["ticker"] == "TST"
    assert row["response"] == envelope  # exactly what the agent was told
    text = canonical_json(envelope)
    assert row["response_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert row["response_bytes"] == len(text.encode()) and row["duration_ms"] == 1.235
    assert row["provenance_ids"] == ["b", "a"]


def test_failed_calls_are_recorded_with_their_error_code(log):
    log.record(tool="x", arguments={}, envelope=_envelope(ok=False, error={"code": "unknown_ticker", "message": "m"}),
               duration_ms=0.1)
    [row] = log.entries()
    assert row["ok"] == 0 and row["error_code"] == "unknown_ticker"


def test_the_log_is_append_only_even_through_a_raw_connection_and_after_reopening(tmp_path):
    path = tmp_path / "calls.db"
    first = CallLog(path)
    first.record(tool="t", arguments={}, envelope=_envelope(), duration_ms=1)
    first.close()
    reopened = CallLog(path)  # re-running the schema must not weaken the triggers
    reopened.record(tool="t", arguments={}, envelope=_envelope(), duration_ms=1)
    reopened.close()
    raw = sqlite3.connect(path)
    for statement in ("UPDATE agent_calls SET ok = 0", "DELETE FROM agent_calls", "UPDATE agent_calls SET response = '{}' WHERE id = 1"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            raw.execute(statement)
    assert raw.execute("SELECT COUNT(*) FROM agent_calls").fetchone()[0] == 2
    raw.close()


def test_huge_arguments_and_hostile_names_are_capped_not_trusted(log):
    log.record(tool="t" * 5_000, arguments={"k": "v" * 50_000}, envelope=_envelope(), duration_ms=1, client="c" * 500)
    [row] = log.entries()
    assert len(row["arguments"]) == MAX_LOGGED_ARGUMENT_CHARS and row["arguments_truncated"] == 1
    assert len(row["tool"]) == 200 and len(row["client"]) == 120


def test_concurrent_writers_all_land_with_distinct_ids(log):
    def write():
        for _ in range(25):
            log.record(tool="t", arguments={}, envelope=_envelope(), duration_ms=1)

    threads = [threading.Thread(target=write) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = [row["id"] for row in log.entries(limit=1_000)]
    assert len(ids) == 200 and len(set(ids)) == 200


def test_entries_filter_by_tool_newest_first(log):
    for tool in ("a", "b", "a"):
        log.record(tool=tool, arguments={}, envelope=_envelope(), duration_ms=1)
    assert [row["id"] for row in log.entries(tool="a")] == [3, 1]
    assert len(log.entries(limit=2)) == 2


# --- the service ---------------------------------------------------------------

def test_the_service_answers_exactly_as_the_tools_do_and_logs_every_call(service, log):
    arguments = {"ticker": "TST"}
    answered = service.handle("get_research_summary", arguments, client="agent-x")
    assert answered == call_tool(service.context, "get_research_summary", arguments)
    service.handle("get_research_summary", {"ticker": "NOPE"})
    service.handle("no_such_tool", {})
    service.handle("get_domain", {"ticker": "TST", "domain": "bogus"})
    rows = list(reversed(log.entries()))
    assert [(r["tool"], r["ok"], r["error_code"]) for r in rows] == [
        ("get_research_summary", 1, None), ("get_research_summary", 0, "unknown_ticker"),
        ("no_such_tool", 0, "unknown_tool"), ("get_domain", 0, "invalid_arguments"),
    ]
    assert rows[0]["client"] == "agent-x" and rows[0]["response"] == answered
    assert rows[0]["provenance_ids"] == answered["provenance_ids"]


def test_a_failing_log_never_changes_or_withholds_the_answer(service, monkeypatch, caplog):
    def broken(**kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(service.log, "record", broken)
    with caplog.at_level(logging.ERROR):
        answered = service.handle("get_methodology", {})
    assert answered["ok"] is True
    assert "could not write the agent call log" in caplog.text


def test_the_service_works_without_a_log(db_url):
    engine = make_readonly_engine(db_url)
    bare = AgentService(AgentContext(engine, load_settings(), lambda: TODAY))
    assert bare.handle("get_methodology", {})["ok"] is True
    engine.dispose()


def test_serving_and_logging_never_touch_the_research_database(db_url, service):
    path = Path(db_url.removeprefix("sqlite:///"))
    before = path.read_bytes()
    for name, args in (("get_research_summary", {"ticker": "TST"}), ("list_universe", {}), ("get_methodology", {})):
        assert service.handle(name, args)["ok"]
    assert path.read_bytes() == before
    assert service.log.path != path


def _script():
    spec = importlib.util.spec_from_file_location("agent_mcp_server_script", ROOT / "scripts" / "agent_mcp_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_server_script_refuses_to_log_into_the_research_database(db_url, tmp_path):
    script = _script()
    research = Path(db_url.removeprefix("sqlite:///"))
    with pytest.raises(ValueError, match="different file"):
        script.build_service(db_url, research)
    built = script.build_service(db_url, tmp_path / "audit.db")
    assert built.handle("get_methodology", {})["ok"]
    assert script.build_service(db_url, None).log is None


def test_the_server_script_explains_a_missing_mcp_package(monkeypatch, capsys):
    script = _script()
    monkeypatch.setitem(sys.modules, "alpha_lab.agent_interface.mcp_adapter", None)
    assert script.main(["--no-log"]) == 2
    assert "pip install" in capsys.readouterr().err


# --- isolation -----------------------------------------------------------------

def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_only_the_mcp_adapter_imports_the_mcp_package():
    for path in ROOT.joinpath("alpha_lab").rglob("*.py"):
        uses_mcp = any(name == "mcp" or name.startswith("mcp.") for name in _imports(path))
        assert uses_mcp == (path.name == "mcp_adapter.py"), path
    package_init = _imports(PACKAGE / "__init__.py")
    assert not any("mcp_adapter" in name for name in package_init)


def test_the_optional_extra_is_declared_outside_the_locked_dependencies():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'mcp = ["mcp==' in text
    assert "mcp==" not in (ROOT / "requirements.lock").read_text()
