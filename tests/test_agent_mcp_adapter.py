"""Agent Research Interface, PR B: the MCP adapter. Needs the optional `mcp`
extra, so it is skipped where that is not installed (the locked CI install)."""

import hashlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pytest.importorskip("mcp")
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy.orm import Session

from alpha_lab.agent_interface import (
    TOOLS,
    AgentContext,
    make_readonly_engine,
)
from alpha_lab.agent_interface.call_log import CallLog
from alpha_lab.agent_interface.mcp_adapter import build_server
from alpha_lab.agent_interface.service import AgentService
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security

ROOT = Path(__file__).resolve().parents[1]
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
def served(db_url, tmp_path):
    engine = make_readonly_engine(db_url)
    log = CallLog(tmp_path / "calls.db")
    service = AgentService(AgentContext(engine, load_settings(), lambda: TODAY), log)
    yield service, log
    log.close()
    engine.dispose()


def _run(coro):
    return anyio.run(coro)


def test_every_registry_tool_is_listed_read_only_with_its_schema(served):
    service, _ = served

    async def go():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            return (await client.list_tools()).tools

    tools = _run(go)
    assert {tool.name for tool in tools} == set(TOOLS)
    for tool in tools:
        assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
        assert tool.inputSchema["type"] == "object" and tool.inputSchema.get("additionalProperties") is False


def test_a_call_returns_the_envelope_and_is_logged_with_the_client_name(served):
    service, log = served

    async def go():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            return await client.call_tool("get_research_summary", {"ticker": "TST"})

    result = _run(go)
    envelope = json.loads(result.content[0].text)
    assert result.isError is False and envelope["ok"] is True and envelope["ticker"] == "TST"
    [row] = log.entries()
    assert row["tool"] == "get_research_summary" and row["response"] == envelope
    assert row["client"]  # the MCP client's own name
    # the wire text is exactly what was hashed, so a response is verifiable against its log row
    assert hashlib.sha256(result.content[0].text.encode()).hexdigest() == row["response_sha256"]


def test_malformed_and_unknown_calls_get_structured_envelopes_and_are_still_logged(served):
    service, log = served

    async def go():
        async with create_connected_server_and_client_session(build_server(service)) as client:
            return [
                await client.call_tool("get_research_summary", {"nope": 1}),
                await client.call_tool("get_research_summary", {"ticker": "ZZZ"}),
                await client.call_tool("no_such_tool", {}),
            ]

    results = _run(go)
    codes = [json.loads(r.content[0].text)["error"]["code"] for r in results]
    assert codes == ["invalid_arguments", "unknown_ticker", "unknown_tool"]
    assert [r["error_code"] for r in reversed(log.entries())] == codes


def test_the_real_stdio_server_speaks_mcp_and_writes_the_log(db_url, tmp_path):
    log_path = tmp_path / "stdio_calls.db"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "agent_mcp_server.py"), "--db", db_url, "--log", str(log_path)],
        cwd=str(ROOT),
    )

    async def go():
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("get_methodology", {})
            return [tool.name for tool in tools.tools], json.loads(result.content[0].text)

    names, envelope = _run(go)
    assert set(names) == set(TOOLS) and envelope["ok"] is True
    audit = CallLog(log_path)
    [row] = audit.entries()
    assert row["tool"] == "get_methodology" and row["response"] == envelope
    audit.close()
