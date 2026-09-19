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

`run_core_refresh` itself only isolates a per-ticker *provider* failure
(`ProviderError`, mirroring `scripts/load_us_data.py`'s established
`_ingest_universe` pattern) and a `rebuild_current_research` failure --
both land on the returned `CoreRefreshResult`, never raised. An
infrastructure-level failure outside those two paths (e.g. the database
itself becoming unreachable mid-loop) is deliberately left to propagate
out of `run_core_refresh`, matching that same precedent. This script's
own contract is broader than that, though: it promises to launch
Streamlit "regardless" of the refresh's outcome, so the `run_core_refresh`
call below is wrapped in its own broad exception handler -- the one place
that promise is actually kept -- rather than launch.py silently relying
on a narrower guarantee `run_core_refresh` never made.

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
            try:
                result = run_core_refresh(engine, settings)
            except Exception as error:  # noqa: BLE001 -- this script's own stated
                # contract is "launch Streamlit regardless of refresh outcome";
                # run_core_refresh only guarantees that for a per-ticker provider
                # failure or a rebuild failure (both captured on the result
                # instead of raised), never for an infrastructure-level failure
                # outside those paths, so that broader promise is kept here.
                print(f"Core refresh failed unexpectedly ({error}) -- launching with whatever current research was already persisted.")
            else:
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
