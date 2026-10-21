#!/usr/bin/env python
"""Check an agent's ResearchNote against what the agent was actually served.

    python scripts/validate_research_note.py note.json [--log data/agent_calls.db]
        [--since 2026-10-20T00:00:00+00:00] [--until ...] [--client claude-desktop]

Reads the append-only call log (opened read-only) and prints a JSON report. A
note is rejected when it cites evidence or provenance that was never served for
its ticker and as-of date, quotes a figure that differs from the served value,
or states a data freshness other than the one it was given. Exit code 0 when
valid, 1 when rejected, 2 when the note or log cannot be read.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.agent_interface.call_log import CallLog
from alpha_lab.agent_interface.research_note import validate_note
from alpha_lab.agent_interface.service import default_log_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("note", type=Path, help="Path to the ResearchNote JSON")
    parser.add_argument("--log", type=Path, default=default_log_path(), help="Call log (append-only SQLite)")
    parser.add_argument("--since", type=datetime.fromisoformat, help="Only calls at or after this UTC time")
    parser.add_argument("--until", type=datetime.fromisoformat, help="Only calls at or before this UTC time")
    parser.add_argument("--client", help="Only calls from this MCP client name")
    options = parser.parse_args(argv)
    try:
        text = options.note.read_text()
        log = CallLog(options.log, read_only=True)
    except (OSError, FileNotFoundError) as error:
        print(f"cannot read: {error}", file=sys.stderr)
        return 2
    try:
        report = validate_note(text, log, since=options.since, until=options.until, client=options.client)
    finally:
        log.close()
    print(json.dumps(report.model_dump(mode="json"), indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
