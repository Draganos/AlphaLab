#!/usr/bin/env python
"""Explicitly rebuild and persist current/live research; UI pages are read-only."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.screener import MarketScreenerService  # noqa: E402


def main() -> None:
    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        create_schema(engine)
        records = MarketScreenerService(engine, settings).rebuild_current_research()
        if not records:
            print("No securities available for current research rebuild")
            return
        print(
            f"Persisted current research for {len(records)} securities; "
            f"evaluation={records[0].evaluation_date}; "
            f"version={records[0].rating_version}"
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
