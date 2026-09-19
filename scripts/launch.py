#!/usr/bin/env python
"""Launch the dashboard, auto-refreshing core data first only if it's stale.

Checks the already-tracked universe's latest stored price observation
against the configured `stale_price_days` observation limit (a pure
database read, no network). If every tracked security is fresh, this does
nothing and launches Streamlit immediately. If anything is stale, it runs
the canonical core refresh (`alpha_lab.refresh.run_core_refresh`: price/
fundamental ingestion + current-research rebuild) once, then launches
Streamlit regardless of whether that refresh fully succeeded -- a failed
or partial refresh never blocks the dashboard from starting with whatever
valid data is already persisted (see `run_core_refresh`'s own docstring
for why a failure here can never corrupt that persisted state).

Deliberately outside Streamlit's own process: `main.py` is rerun by
Streamlit on every widget interaction, and this staleness check/refresh
must run exactly once, before the server starts -- not on every rerun.
The dashboard's own "Full Refresh" button is the in-app equivalent for a
session already running, calling the same `run_core_refresh` explicitly
on click rather than automatically.

Usage: `python scripts/launch.py [-- streamlit-args...]`, e.g.
`python scripts/launch.py -- --server.headless true`.
"""

from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.refresh import is_universe_price_stale, run_core_refresh  # noqa: E402

_DASHBOARD_ENTRYPOINT = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "main.py"


def main() -> None:
    extra_args = sys.argv[1:]
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        create_schema(engine)
        stale_after_days = settings.data_quality["stale_price_days"]
        if is_universe_price_stale(engine, stale_after_days):
            print(
                f"Tracked universe has price data older than the configured "
                f"{stale_after_days}-day observation limit -- running core refresh "
                f"(price/fundamental ingestion + research rebuild) before launch."
            )
            result = run_core_refresh(engine, settings)
            print(
                f"Core refresh: {len(result.tickers_succeeded)}/"
                f"{len(result.tickers_attempted)} ticker(s) ingested "
                f"({len(result.tickers_failed)} failed)."
            )
            if not result.ok:
                print(
                    f"Research rebuild failed ({result.research_error}) -- launching "
                    f"with whatever current research was already persisted."
                )
            else:
                print(f"Research rebuilt for {result.research_record_count} securit(y/ies).")
        else:
            print("Tracked universe's price data is within the configured observation limit; skipping refresh.")
    finally:
        engine.dispose()

    os.execvp(
        sys.executable,
        [sys.executable, "-m", "streamlit", "run", str(_DASHBOARD_ENTRYPOINT), *extra_args],
    )


if __name__ == "__main__":
    main()
