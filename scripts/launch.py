#!/usr/bin/env python
"""Launch the dashboard, auto-refreshing core data first only if it's
stale, then establishing a versioned research-state stamp.

Checks the already-tracked universe's latest stored price observation
against the configured `stale_price_days` observation limit (a pure
database read, no network). If every tracked security is fresh, core
data is never re-ingested -- exactly as before the Research Refresh
Orchestrator (roadmap Phase 3) existed; see
`alpha_lab.research_refresh.ResearchRefreshOrchestrator._maybe_refresh_
core`'s own docstring for why this cost profile must stay unchanged.
Either way, the orchestrator then reads (never refreshes) every other
evidence domain's current coverage/freshness across the tracked
universe -- Analyst Consensus/Technical/AI Research Rating, News, Macro
Regime, Donatien, Alignment -- and persists a versioned "research
state" stamp any future AI consumer can cite (see that module's own
docstring for the full design). Streamlit launches regardless of
whether any of this fully succeeded -- a failed or partial refresh
never blocks the dashboard from starting with whatever valid data is
already persisted.

`run_core_refresh` (called internally by the orchestrator, completely
unchanged) only isolates a per-ticker *provider* failure (`ProviderError`,
mirroring `scripts/load_us_data.py`'s established `_ingest_universe`
pattern) and a `rebuild_current_research` failure -- both land on the
returned result, never raised. An infrastructure-level failure outside
those two paths (e.g. the database itself becoming unreachable mid-loop)
is deliberately left to propagate, matching that same precedent. This
script's own contract is broader than that, though: it promises to
launch Streamlit "regardless" of the refresh's outcome, so the
orchestrator call below is wrapped in its own broad exception handler --
the one place that promise is actually kept -- rather than launch.py
silently relying on a narrower guarantee the orchestrator never made.

Deliberately outside Streamlit's own process: `main.py` is rerun by
Streamlit on every widget interaction, and this staleness check/refresh
must run exactly once, before the server starts -- not on every rerun.
The dashboard's own "Full Refresh" button is the in-app equivalent for a
session already running, calling the same orchestrator explicitly on
click (with `force_core=True`) rather than automatically.

Usage: `python scripts/launch.py [-- streamlit-args...]`, e.g.
`python scripts/launch.py -- --server.headless true`.
"""

from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.research_refresh import ResearchRefreshOrchestrator  # noqa: E402

_DASHBOARD_ENTRYPOINT = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "main.py"


def main() -> None:
    extra_args = sys.argv[1:]
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        create_schema(engine)
        try:
            status = ResearchRefreshOrchestrator(engine, settings).run()
        except Exception as error:  # noqa: BLE001 -- this script's own stated
            # contract is "launch Streamlit regardless of refresh outcome"; the
            # orchestrator only guarantees that for a per-ticker provider
            # failure or a rebuild failure (both captured on its result instead
            # of raised), never for an infrastructure-level failure outside
            # those paths, so that broader promise is kept here.
            print(f"Research refresh failed unexpectedly ({error}) -- launching with whatever current research was already persisted.")
        else:
            if status.core.skipped:
                print("Tracked universe's price data is within the configured observation limit; core refresh skipped.")
            else:
                print(
                    f"Core refresh: {status.core.tickers_succeeded}/"
                    f"{status.core.tickers_attempted} ticker(s) ingested "
                    f"({status.core.tickers_failed} failed)."
                )
                if status.core.research_error is not None:
                    print(
                        f"Research rebuild failed ({status.core.research_error}) -- launching "
                        f"with whatever current research was already persisted."
                    )
            print(f"Research state established: version {status.version_id[:12]}, {status.tracked_universe_size} tracked securit(y/ies).")
    finally:
        engine.dispose()

    os.execvp(
        sys.executable,
        [sys.executable, "-m", "streamlit", "run", str(_DASHBOARD_ENTRYPOINT), *extra_args],
    )


if __name__ == "__main__":
    main()
