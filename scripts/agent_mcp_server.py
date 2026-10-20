#!/usr/bin/env python
"""Serve the read-only Agent Research Interface over MCP (stdio).

    pip install "alpha-lab[mcp]"
    python scripts/agent_mcp_server.py [--db sqlite:///data/alpha_lab.db] [--log data/agent_calls.db]

Example client configuration (Claude Desktop style):

    {"mcpServers": {"alphalab": {"command": "python",
        "args": ["/path/to/AlphaLab/scripts/agent_mcp_server.py"]}}}

The research database is opened read-only; every call is appended to a
separate audit log (`--log`, default data/agent_calls.db; `--no-log` to
disable). stdout carries the protocol, so diagnostics go to stderr.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.agent_interface import AgentContext, make_readonly_engine
from alpha_lab.agent_interface.call_log import CallLog
from alpha_lab.agent_interface.service import (
    AgentService,
    default_log_path,
)
from alpha_lab.config import load_settings


def build_service(database_url: str, log_path: Path | None) -> AgentService:
    settings = load_settings()
    url = database_url or settings.database_url
    engine = make_readonly_engine(url)
    research_file = Path(url.removeprefix("sqlite:///")).resolve()
    if log_path is not None and log_path.resolve() == research_file:
        raise ValueError("the audit log must be a different file from the research database")
    log = None if log_path is None else CallLog(log_path)
    return AgentService(AgentContext(engine=engine, settings=settings), log)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="", help="SQLite URL of the research database (default: configured)")
    parser.add_argument("--log", type=Path, default=default_log_path(), help="Audit log file (append-only SQLite)")
    parser.add_argument("--no-log", action="store_true", help="Do not write an audit log")
    options = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    try:
        import anyio

        from alpha_lab.agent_interface.mcp_adapter import serve_stdio
    except ImportError:
        print('The MCP package is not installed: pip install "alpha-lab[mcp]"', file=sys.stderr)
        return 2
    try:
        service = build_service(options.db, None if options.no_log else options.log)
    except (ValueError, FileNotFoundError) as error:
        print(f"cannot start: {error}", file=sys.stderr)
        return 2
    anyio.run(serve_stdio, service)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
