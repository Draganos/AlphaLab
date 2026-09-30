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
from alpha_lab.providers.errors import ProviderErrorKind
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

# Safety cap for run_core_refresh's own "tickers=None" (whole configured
# universe) path -- both scripts/launch.py's launch-time check and the
# manual Full Refresh button take this path by omitting `tickers`. Real
# incident that motivated this: a broadly-loaded universe (e.g. `scripts/
# load_universe.py`'s full NASDAQ/NYSE non-ETF directory -- thousands of
# tickers, since that script's own `--limit` only bounds live metadata
# *enrichment*, not how many `Security` rows `UniverseIngestionService.
# load` creates) turned one Full Refresh click into a many-hour blocking
# operation with zero progress feedback -- indistinguishable from "stuck"
# to whoever was waiting on it.
#
# Kept equal to MAX_AUTO_REFRESH_TICKERS deliberately: both bound the same
# underlying cost (one live provider round-trip per ticker, none of it
# backgrounded or parallelized) for a caller not already restricting to an
# explicit subset. Unlike the automatic trigger (which skips entirely
# above its cap, leaving the manual button as the deliberate fallback),
# there is no smaller/safer fallback below this one, so above this many
# tracked tickers `run_core_refresh` switches from "ingest the whole
# universe every call" (fine at the tens-of-tickers scale every caller
# here was originally sized for) to "ingest the
# MAX_FULL_UNIVERSE_REFRESH_BATCH stalest tickers this call, the rest on
# the next call." Self-correcting, no persisted cursor needed: each call
# ingests the current staleness leaders (`stale_universe_tickers`' own
# alphabetical-by-ticker order), which removes them from the *next*
# call's stale set, so repeated calls work through the backlog
# deterministically without ever double-processing an already-fresh
# ticker.
MAX_FULL_UNIVERSE_REFRESH_BATCH = MAX_AUTO_REFRESH_TICKERS

# Circuit breaker for one ingestion batch. When Yahoo starts throttling (or
# the network is down) every further ticker just burns its own retries and
# backoff and fails the same way -- a 200-ticker batch then runs for tens of
# minutes while clearing almost nothing (observed: ~30 min, ~50 tickers).
# After this many *consecutive* RATE_LIMITED/NETWORK_UNAVAILABLE failures the
# batch stops and reports the untouched remainder; any success resets the
# count, and NO_DATA / unclassified per-ticker failures never count (those
# are about that ticker, not the provider).
MAX_CONSECUTIVE_PROVIDER_FAILURES = 5
_BREAKER_KINDS = frozenset({ProviderErrorKind.RATE_LIMITED, ProviderErrorKind.NETWORK_UNAVAILABLE})


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
    """The live research universe `run_core_refresh` ingests -- every
    `Security` with `is_tracked=True`, exactly what `MarketScreenerService.
    build_live_records` itself reads (see that method's own `select(
    Security)` queries), so a core refresh ingests precisely what the
    rebuild step is about to use. A `Security` row can exist without being
    tracked (`scripts/load_universe.py`'s broad catalog load never sets
    the flag -- see `Security`'s own docstring); this never discovers or
    expands the tracked universe on its own. Adding/removing a ticker from
    it is `scripts/manage_universe.py`'s job."""
    with Session(engine) as session:
        return list(session.scalars(
            select(Security.ticker).where(Security.is_tracked.is_(True)).order_by(Security.ticker)
        ))


class UniverseTooLargeToAdopt(ValueError):
    """Adopting the latest research build would leave a tracked universe
    larger than any curated research universe (see
    `MAX_FULL_UNIVERSE_REFRESH_BATCH`)."""

    def __init__(self, count: int, cap: int, *, what: str = "the latest research build"):
        super().__init__(
            f"{what} holds {count} securities (cap {cap}): that is the pre-tracking 'every security "
            "in the table' universe, not a curated research universe. "
            "Choose the tickers explicitly: `manage_universe.py set-tracked TICKER ...`."
        )
        self.count, self.cap = count, cap


def adopt_current_research_tickers(engine: Engine, *, max_tickers: int = MAX_FULL_UNIVERSE_REFRESH_BATCH) -> list[str]:
    """Mark every ticker in the latest current research build as tracked;
    returns the tickers newly marked (already-tracked ones are untouched, so
    this only ever adds and is idempotent).

    Repair for a database migrated before `create_schema` backfilled
    `is_tracked`: there every pre-existing security became untracked, so
    `configured_universe_tickers` was empty and Full Refresh silently did
    nothing. Deliberately an explicit action (`scripts/manage_universe.py
    adopt-current`), never automatic: an empty tracked universe can also be
    a deliberate result of `manage_universe remove`, and a rebuild with no
    tracked securities is not persisted, so the latest build would
    otherwise resurrect removed tickers.

    Refuses (`UniverseTooLargeToAdopt`) when either the latest build's total
    size or the tracked universe that adoption would leave (already-tracked
    plus newly adopted) exceeds `max_tickers`: on the first real database
    this was run against, the latest build held 5,149 securities -- the
    pre-tracking universe that `is_tracked` exists to end -- so adopting
    "whatever the build had" recreated exactly that. Bounding only the
    untracked candidates would let a build of 200 already-tracked plus 200
    untracked securities through and grow the universe to 400."""
    from alpha_lab.database.models import CurrentResearchBuild, CurrentResearchSnapshot

    with Session(engine) as session:
        build_id = session.scalar(select(func.max(CurrentResearchBuild.id)))
        if build_id is None:
            return []
        build_size = session.scalar(
            select(func.count()).select_from(CurrentResearchSnapshot).where(CurrentResearchSnapshot.build_id == build_id)
        ) or 0
        if build_size > max_tickers:
            raise UniverseTooLargeToAdopt(build_size, max_tickers)
        candidates = session.scalars(
            select(Security)
            .join(CurrentResearchSnapshot, CurrentResearchSnapshot.ticker == Security.ticker)
            .where(CurrentResearchSnapshot.build_id == build_id, Security.is_tracked.is_(False))
            .order_by(Security.ticker)
        ).all()
        tracked_now = session.scalar(
            select(func.count()).select_from(Security).where(Security.is_tracked.is_(True))
        ) or 0
        if tracked_now + len(candidates) > max_tickers:
            raise UniverseTooLargeToAdopt(
                tracked_now + len(candidates), max_tickers, what="the tracked universe after adoption"
            )
        adopted = [security.ticker for security in candidates]
        for security in candidates:
            security.is_tracked = True
        session.commit()
    return adopted


@dataclass
class SetTrackedResult:
    tracked: list[str]
    newly_tracked: list[str]
    untracked_count: int
    missing: list[str]


def set_tracked_tickers(engine: Engine, tickers: list[str]) -> SetTrackedResult:
    """Make the tracked universe exactly `tickers`: flag them tracked and
    every other tracked security untracked. Only flags change -- no
    `Security` row or any history is ever deleted, and nothing is fetched.
    A ticker with no `Security` row is reported in `missing` (it needs
    `manage_universe.py add`, which ingests it). Raises `ValueError` rather
    than leave the universe empty when none of `tickers` exist."""
    wanted = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip()))
    with Session(engine) as session:
        existing = {
            security.ticker: security
            for security in session.scalars(select(Security).where(Security.ticker.in_(wanted)))
        }
        if not existing:
            raise ValueError("None of the requested tickers exist; refusing to leave the tracked universe empty.")
        newly_tracked = sorted(t for t, sec in existing.items() if not sec.is_tracked)
        for security in existing.values():
            security.is_tracked = True
        others = session.scalars(
            select(Security).where(Security.is_tracked.is_(True), Security.ticker.not_in(list(existing)))
        ).all()
        for security in others:
            security.is_tracked = False
        session.commit()
        return SetTrackedResult(
            tracked=sorted(existing), newly_tracked=newly_tracked,
            untracked_count=len(others), missing=[t for t in wanted if t not in existing],
        )


def filing_eligible_tickers(engine: Engine) -> list[str]:
    """Tracked tickers for which SEC 10-K/10-Q ingestion is meaningful:
    every `configured_universe_tickers` entry except funds, which file
    neither (see `alpha_lab.research.security_type` -- AI Research is
    structurally not-applicable to an ETF). This is the default target of
    `scripts/refresh_company_documents.py`; a tracked equity with no
    ingested documents otherwise shows a permanently empty AI Research
    category with nothing in the workflow ever prompting to fill it."""
    from alpha_lab.research.security_type import SecurityType, normalize_security_type

    with Session(engine) as session:
        rows = session.execute(
            select(Security.ticker, Security.asset_type)
            .where(Security.is_tracked.is_(True))
            .order_by(Security.ticker)
        ).all()
    return [
        ticker for ticker, asset_type in rows
        if normalize_security_type(asset_type) is not SecurityType.ETF
    ]


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
    # Set when the ingestion loop stopped early (see
    # MAX_CONSECUTIVE_PROVIDER_FAILURES); `tickers_not_attempted` is then the
    # untouched remainder of the batch. Both empty on a complete batch.
    stopped_early: str | None = None
    tickers_not_attempted: list[str] = field(default_factory=list)

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
    universe -- unless that universe exceeds `MAX_FULL_UNIVERSE_REFRESH_
    BATCH`, in which case it ingests only that many of the currently
    stalest tickers instead (see that constant's own docstring for why:
    a broadly-loaded universe would otherwise turn one call into a
    many-hour blocking operation). Below the cap this is byte-identical
    to before this batching existed. Either way, the research rebuild
    step below always runs against the full current database state: it
    is a local read/recompute, not a network call, so narrowing its scope
    to match a partial ingestion would only leave already-fresh tickers'
    research stale for no reason.

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
    if tickers is None:
        target_tickers = configured_universe_tickers(engine)
        if len(target_tickers) > MAX_FULL_UNIVERSE_REFRESH_BATCH:
            stale_after_days = settings.data_quality["stale_price_days"]
            latest_by_ticker = _latest_price_by_ticker(engine)
            # Stalest first: a ticker with no price at all (never fetched)
            # before one merely a few days old. The sort is stable, so ties
            # keep the universe's own order.
            target_tickers = sorted(
                stale_universe_tickers(engine, stale_after_days),
                key=lambda ticker: latest_by_ticker.get(ticker, date.min),
            )[:MAX_FULL_UNIVERSE_REFRESH_BATCH]
    else:
        target_tickers = list(tickers)
    ingestion_service = IngestionService(YFinanceProvider(), engine)
    end = date.today()
    start = end - timedelta(days=365 * years)

    succeeded: list[str] = []
    failed: dict[str, str] = {}
    stopped_early: str | None = None
    not_attempted: list[str] = []
    consecutive_provider_failures = 0
    for index, ticker in enumerate(target_tickers):
        try:
            ingestion_service.ingest(ticker, start, end)
            succeeded.append(ticker)
            consecutive_provider_failures = 0
        except ProviderError as error:
            failed[ticker] = str(error)
            if error.kind in _BREAKER_KINDS:
                consecutive_provider_failures += 1
            else:
                consecutive_provider_failures = 0
            if consecutive_provider_failures >= MAX_CONSECUTIVE_PROVIDER_FAILURES:
                not_attempted = target_tickers[index + 1 :]
                stopped_early = (
                    f"stopped after {consecutive_provider_failures} consecutive "
                    f"{error.kind.value} failures ({error}); wait a few minutes and run again"
                )
                break

    result = CoreRefreshResult(
        tickers_attempted=target_tickers[: len(target_tickers) - len(not_attempted)],
        tickers_succeeded=succeeded,
        tickers_failed=failed,
        stopped_early=stopped_early,
        tickers_not_attempted=not_attempted,
    )
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
