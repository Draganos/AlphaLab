"""Canonical "core refresh" operation: price/fundamental ingestion for the
already-configured universe, followed by a current-research rebuild.

This is deliberately the ONE code path behind the launch-time auto-refresh
check (`scripts/launch.py`), the main dashboard's own automatic
on-session-start refresh of whatever is currently stale, and its manual
"Full Refresh" button (all in `app/dashboard/main.py`) -- none of these
callers re-implement ingestion or research-building logic; all call
`run_core_refresh` directly, the automatic dashboard trigger passing
`stale_universe_tickers`'s result so it re-ingests only what is actually
stale (keeping that automatic trigger's cost proportional to the problem)
while the launch check and the button both omit `tickers` for the full
configured universe. "Core" is scoped narrowly on purpose: price/fundamental data via
`alpha_lab.ingestion.IngestionService` plus `MarketScreenerService.
rebuild_current_research()`, never the supplemental research domains
(Analyst Consensus/Technical/AI Research/News/Macro Regime/Donatien
External Calibration) -- those stay independently refreshable through
their own existing `scripts/refresh_*.py` mechanisms, deliberately out of
scope here to keep this operation atomic, synchronous, and predictable
within one call (no partially-refreshed state, no backgrounded provider
calls, no ambiguity about when the UI is consistent again). This does not
by itself prevent two independent callers -- e.g. two browser sessions'
Full Refresh buttons, or a Full Refresh click racing the launch-time
check -- from running concurrently; see `run_core_refresh_guarded`'s own
docstring for exactly what its in-progress guard does and does not cover.

`is_universe_price_stale`/`stale_universe_tickers` are pure database reads
(no network) -- safe to call on every Streamlit render, exactly like every
other read in this codebase's explicit-refresh architecture. `run_core_
refresh` is the one function that calls a provider; every caller must
invoke it deliberately (a pre-launch check, an explicit button click, or
the dashboard's own once-per-session automatic trigger guarded by
`st.session_state`), never as an unconditional side effect of rendering a
page -- the automatic trigger still only ever calls it once per browser
session (see `app/dashboard/main.py`'s own guard), not on every rerun.
"""

from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alpha_lab.config import Settings
from alpha_lab.data_quality import assess_freshness
from alpha_lab.database.models import Price, Security
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers import ProviderError, YFinanceProvider
from alpha_lab.screener import MarketScreenerService

DEFAULT_INGESTION_YEARS = 2

# Safety cap on the dashboard's automatic on-session-start refresh (see
# app/dashboard/main.py): if more tickers are stale than this, the
# automatic trigger is skipped entirely rather than silently attempting
# to ingest all of them. Each ticker costs one live provider round-trip
# (company info + price history + financials) plus, inside
# IngestionService.ingest, one database query per stored price row for
# its upsert check -- neither is backgrounded or parallelized, so a
# stale count in the thousands (e.g. the whole universe going stale at
# once, or a freshly-loaded large universe) can turn what was meant to be
# a quick, proportional top-up into a multi-hour blocking page load, the
# opposite of this trigger's own "loading time doesn't increase
# substantially" design goal. Above this cap, the existing stale-data
# warning and manual Full Refresh button remain the way to catch up --
# refreshing everything is still possible, just never silently automatic.
MAX_AUTO_REFRESH_TICKERS = 200


def _latest_price_by_ticker(engine: Engine) -> dict[str, date]:
    """One row per ticker via SQL `MAX(date)` -- this runs unconditionally
    on every dashboard render (`is_universe_price_stale` is called at
    module scope on every Streamlit rerun), so it deliberately never pulls
    a ticker's full price history into Python just to find its latest
    date."""
    with Session(engine) as session:
        rows = session.execute(
            select(Price.ticker, func.max(Price.date)).group_by(Price.ticker)
        ).all()
    return dict(rows)


def configured_universe_tickers(engine: Engine) -> list[str]:
    """The already-tracked universe `run_core_refresh` ingests -- exactly
    the tickers `MarketScreenerService.build_live_records` itself reads
    from `Security` (see that method's own `select(Security)` query), so
    a core refresh ingests precisely what the rebuild step is about to
    use. Never discovers or expands the universe (that remains
    `scripts/load_universe.py`/`load_live_research.py`'s job)."""
    with Session(engine) as session:
        return list(session.scalars(select(Security.ticker).order_by(Security.ticker)))


def stale_universe_tickers(
    engine: Engine, stale_after_days: int, *, evaluation_date: date | None = None
) -> list[str]:
    """Exactly which already-tracked tickers have a latest stored price
    observation that is missing or older than `stale_after_days` -- the
    same staleness rule `is_universe_price_stale` uses, but returning the
    actual subset (in `configured_universe_tickers`'s own order) rather
    than a single boolean, so a caller can refresh only what is actually
    stale (e.g. the dashboard's automatic on-session-start refresh) instead
    of paying for the entire universe every time. Pure database read, no
    network. An empty universe returns an empty list -- see
    `is_universe_price_stale`'s docstring for why that is never "stale"."""
    evaluation_date = evaluation_date or date.today()
    tickers = configured_universe_tickers(engine)
    if not tickers:
        return []
    latest_by_ticker = _latest_price_by_ticker(engine)
    return [
        ticker
        for ticker in tickers
        if assess_freshness(
            "price", latest_by_ticker.get(ticker), evaluation_date, stale_after_days
        )
        is not None
    ]


def is_universe_price_stale(
    engine: Engine, stale_after_days: int, *, evaluation_date: date | None = None
) -> bool:
    """True when any already-tracked security's latest stored price
    observation is missing or older than `stale_after_days` -- pure
    database read, no network. An empty universe (nothing tracked yet)
    is never "stale": there is nothing here for a core refresh to fix,
    that is a separate bootstrapping concern (`scripts/load_universe.py`).
    """
    return bool(
        stale_universe_tickers(engine, stale_after_days, evaluation_date=evaluation_date)
    )


@dataclass
class CoreRefreshResult:
    tickers_attempted: list[str]
    tickers_succeeded: list[str] = field(default_factory=list)
    tickers_failed: dict[str, str] = field(default_factory=dict)
    research_rebuilt: bool = False
    research_record_count: int = 0
    # None means the rebuild step itself never raised. Ingestion failures
    # for individual tickers are never fatal (see `tickers_failed`) and
    # never prevent the rebuild step from running on whatever data is
    # already present -- only the rebuild step itself raising is captured
    # here.
    research_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.research_rebuilt and self.research_error is None


def run_core_refresh(
    engine: Engine,
    settings: Settings,
    *,
    years: int = DEFAULT_INGESTION_YEARS,
    tickers: list[str] | None = None,
) -> CoreRefreshResult:
    """The canonical core refresh: ingest price/fundamental data for the
    configured universe (one ticker's *provider* failure never aborts the
    rest, mirroring `scripts/load_us_data.py`'s established `_ingest_
    universe` pattern exactly -- only `ProviderError` is caught per ticker;
    an infrastructure-level failure, e.g. the database connection itself
    being unusable, is deliberately left to propagate rather than silently
    continuing to attempt more writes against it), then rebuild current
    research from whatever is now stored.

    `tickers`, when given, restricts ingestion to exactly that subset of
    the configured universe -- e.g. the dashboard's automatic
    on-session-start refresh, which passes only `stale_universe_tickers`'s
    result so that trigger's cost stays proportional to what is actually
    stale, rather than always re-ingesting the entire universe. Omitting
    it (the default -- used by both `scripts/launch.py`'s launch-time
    check and the manual Full Refresh button) ingests the full configured
    universe, exactly as before this parameter existed. Either way, the
    research rebuild step below always runs against the full current
    database state: it is a local read/recompute, not a network call, so
    narrowing its scope to match a partial ingestion would only leave
    already-fresh tickers' research stale for no reason.

    Never raises for a per-ticker *provider* failure. `MarketScreenerService.
    rebuild_current_research`'s own failure is caught and reported on the
    result rather than propagated -- `Phase3Repository.save_current_research`
    persists one immutable build atomically and validates before opening a
    session, so a failed rebuild here can never leave a partially-written
    or corrupted current-research state; the previously persisted build
    (if any) simply remains exactly as it was. Callers (the launcher, the
    Full Refresh button) decide how to surface `research_error`, never how
    to recover the data -- there is nothing to recover.
    """
    target_tickers = configured_universe_tickers(engine) if tickers is None else list(tickers)
    ingestion_service = IngestionService(YFinanceProvider(), engine)
    end = date.today()
    start = end - timedelta(days=365 * years)

    succeeded: list[str] = []
    failed: dict[str, str] = {}
    for ticker in target_tickers:
        try:
            ingestion_service.ingest(ticker, start, end)
            succeeded.append(ticker)
        except ProviderError as error:
            failed[ticker] = str(error)

    result = CoreRefreshResult(tickers_attempted=target_tickers, tickers_succeeded=succeeded, tickers_failed=failed)
    try:
        records = MarketScreenerService(engine, settings).rebuild_current_research()
        result.research_rebuilt = True
        result.research_record_count = len(records)
    except Exception as error:  # noqa: BLE001 -- see docstring: must never propagate into a
        # crashed launcher/page; the atomic, validate-before-write persistence this wraps
        # already guarantees no partial state, so reporting is the only remaining job here.
        result.research_error = str(error)
    return result


def run_core_refresh_guarded(
    engine: Engine,
    settings: Settings,
    state: MutableMapping[str, object],
    *,
    years: int = DEFAULT_INGESTION_YEARS,
    tickers: list[str] | None = None,
) -> CoreRefreshResult | None:
    """Same operation as `run_core_refresh` (see its own docstring for what
    `tickers` restricts), refusing to start a second one while `state`
    already records one in progress -- `state` is typically Streamlit's
    `st.session_state`, which persists across reruns for the same browser
    session, so a refresh started by one button click stays recorded
    across the rerun that click triggers. Returns `None` without calling
    any provider when a refresh is already in progress, instead of
    starting an overlapping one; the in-progress flag is always cleared
    again before returning, on both success and failure, so a crashed
    refresh never permanently locks out future attempts.

    This guard is scoped to whatever single `state` mapping is passed in --
    `st.session_state` is per browser session, so it prevents a double
    click or a rerun re-entering this function within one session, but it
    does NOT prevent two different sessions (two users, or the launch-time
    check racing a button click) from each starting their own core refresh
    at the same time. Cross-session/cross-process mutual exclusion is out
    of scope here deliberately -- see the module docstring."""
    if state.get("core_refresh_in_progress"):
        return None
    state["core_refresh_in_progress"] = True
    try:
        return run_core_refresh(engine, settings, years=years, tickers=tickers)
    finally:
        state["core_refresh_in_progress"] = False
