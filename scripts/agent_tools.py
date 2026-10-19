#!/usr/bin/env python
"""Call the Agent Research Interface (roadmap Phase 9) from the command line.

    python scripts/agent_tools.py list
    python scripts/agent_tools.py call get_research_summary --args '{"ticker": "MSFT"}'
    python scripts/agent_tools.py call get_methodology --db sqlite:///data/alpha_lab.db

Read-only by construction: the database is opened read-only, nothing is
fetched from any provider, and nothing is written. The JSON envelope is
printed to stdout; the exit code is non-zero when the tool reports ok=false.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.agent_interface import (
    AgentContext,
    call_tool,
    make_readonly_engine,
    tool_catalog,
)
from alpha_lab.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Print the tool catalogue (names, descriptions, input schemas)")
    call = sub.add_parser("call", help="Call one tool and print its JSON envelope")
    call.add_argument("tool")
    call.add_argument("--args", default="{}", help="Tool arguments as a JSON object")
    call.add_argument("--db", help="SQLite URL (default: the configured database)")
    options = parser.parse_args(argv)

    if options.command == "list":
        print(json.dumps(tool_catalog(), indent=2))
        return 0
    try:
        arguments = json.loads(options.args)
        if not isinstance(arguments, dict):
            raise TypeError("--args must be a JSON object")
    except (ValueError, TypeError) as error:
        print(f"invalid --args: {error}", file=sys.stderr)
        return 2
    settings = load_settings()
    engine = make_engine_for(options.db or settings.database_url)
    envelope = call_tool(AgentContext(engine=engine, settings=settings), options.tool, arguments)
    print(json.dumps(envelope, indent=2))
    return 0 if envelope["ok"] else 1


def make_engine_for(database_url: str):
    return make_readonly_engine(database_url)


if __name__ == "__main__":
    raise SystemExit(main())
