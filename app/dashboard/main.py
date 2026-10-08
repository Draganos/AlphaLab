"""AlphaLab Phase 1 Streamlit research dashboard."""

from pathlib import Path
import sys
from datetime import date
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database.models import Price
from alpha_lab.database.session import create_schema, make_engine
from alpha_lab.data_quality import assess_freshness
from alpha_lab.refresh import (
    configured_universe_tickers,
    MAX_AUTO_REFRESH_TICKERS,
    MAX_FULL_UNIVERSE_REFRESH_BATCH,
    is_universe_price_stale,
    stale_universe_tickers,
    upgrade_stale_current_research,
)
from alpha_lab.research_refresh import get_current_research_refresh_status, run_research_refresh_guarded
from alpha_lab.screener import MarketScreenerService
from alpha_lab.search import ScreenCriteria, ScreenRecord, apply_screen

st.set_page_config(page_title="AlphaLab", page_icon="α", layout="wide")
settings = load_settings()
engine = make_engine(settings.database_url)
# Additive, idempotent schema bootstrap/upgrade -- every batch script already
# does this before touching the database; the dashboard previously did not,
# so a database created before a later model was added (e.g. the
# Analyst Consensus / Technical Summary / AI Research / External Calibration
# tables) would 500 with "no such table" here instead of self-healing. Never
# drops or rewrites existing tables/data -- see alpha_lab.database.session.create_schema.
create_schema(engine)


@st.cache_data(ttl=900)
def build_screener() -> pd.DataFrame:
    """Canonical current-research read -- same MarketScreenerService.read_current_research()
    + ScreenRecord/apply_screen mapping used by app/dashboard/pages/3_Market_Screener.py,
    so both pages agree on ticker/company/price/market cap/category scores/overall
    score/coverage/data quality/Sharia status for the same current research build.
    Never rebuilds, never calls a provider, never scores anything itself -- a pure
    database read (see MarketScreenerService.read_current_research's own docstring),
    safe to cache and safe on every page load. Returns an empty DataFrame when no
    current research build has been persisted yet; callers must not fabricate rows
    for that case. This intentionally does not touch HistoricalScoringService --
    that engine remains reserved for the point-in-time backtester."""
    records = MarketScreenerService(engine, settings).read_current_research()
    if not records:
        return pd.DataFrame()
    screen_records = [
        ScreenRecord(
            ticker=item.ticker,
            company_name=item.company,
            country=item.country,
            exchange=item.exchange,
            sector=item.sector,
            industry=item.industry,
            themes=item.themes,
            ethical_status=item.ethical_status,
            overall_score=item.overall_score,
            growth_score=item.category_scores.get("earnings_growth"),
            revisions_score=item.category_scores.get("analyst_revisions"),
            quality_score=item.category_scores.get("business_quality"),
            valuation_score=item.category_scores.get("valuation"),
            momentum_score=item.category_scores.get("momentum"),
            financial_strength_score=item.category_scores.get("financial_strength"),
            ai_research_score=item.category_scores.get("ai_research"),
            shareholder_return_score=item.category_scores.get("shareholder_return"),
            debt_to_ebitda=item.raw_metrics.get("debt_ebitda"),
            market_cap=item.market_cap,
            currency=item.currency,
            market_cap_usd=item.market_cap_usd,
            coverage=item.overall_live_coverage,
        )
        for item in records
    ]
    # Every ethical status is included here (unlike 3_Market_Screener.py's default
    # PASS-only widget) so this summary table never silently drops a security --
    # Sharia Status is displayed as its own column instead.
    all_statuses = ScreenCriteria(ethical_status=["PASS", "REVIEW", "EXCLUDED", "UNKNOWN"])
    selected = apply_screen(screen_records, all_statuses)
    indexed = {item.ticker: item for item in records}
    rows = [
        {
            "Ticker": item.ticker,
            "Company": item.company_name,
            "Price": indexed[item.ticker].price,
            "Market Cap": item.market_cap,
            "Fund AUM": indexed[item.ticker].fund_aum,
            "Fund Category": indexed[item.ticker].fund_category,
            "Sector": item.sector,
            "Industry": item.industry,
            "Overall Rating": item.overall_score,
            "Growth": item.growth_score,
            "Revisions": item.revisions_score,
            "Quality": item.quality_score,
            "Valuation": item.valuation_score,
            "Momentum": item.momentum_score,
            "Financial Strength": item.financial_strength_score,
            "AI Rating": item.ai_research_score,
            "Shareholder Return": item.shareholder_return_score,
            "Coverage": item.coverage,
            "Data Quality": indexed[item.ticker].data_quality_status,
            "Sharia Status": item.ethical_status,
        }
        for item in selected
    ]
    return pd.DataFrame(rows)


_schema_upgrade = upgrade_stale_current_research(engine, settings)
if _schema_upgrade.rebuilt:
    build_screener.clear()
st.title("α AlphaLab")
if _schema_upgrade.message:
    getattr(st, _schema_upgrade.level)(_schema_upgrade.message)
st.caption("Research and paper trading only — no brokerage execution")
st.info("Primary research question: **Is AlphaLab actually outperforming after adjusting for risk and trading costs?**")
left, right = st.columns(2)
left.subheader("CORE PORTFOLIO")
left.write("Tracked separately; AlphaLab does not make allocation recommendations for it.")
right.subheader("SYSTEMATIC EXPERIMENTAL SLEEVE")
right.write(f"Paper starting value setting: AED {settings.paper_trading['initial_value_aed']:,.0f} — a simulation setting, not a recommendation.")

st.divider()
_stale_price_days = settings.data_quality["stale_price_days"]
_staleness_banner = st.empty()
NO_TRACKED_UNIVERSE_MESSAGE = (
    "No securities are tracked, so Full Refresh and the automatic refresh have nothing to "
    "update. If this database predates the tracked-universe model, run "
    "`python scripts/manage_universe.py adopt-current` to re-track the securities in the "
    "latest research build (it refuses a build over 200 securities: pick them with "
    "`set-tracked TICKER ...`); otherwise `python scripts/manage_universe.py add TICKER ...`."
)


def _render_staleness_banner() -> None:
    # A placeholder rather than a plain st.warning/st.caption call so this
    # can be re-rendered in place after a same-run Full Refresh below --
    # otherwise this banner (computed before the button's own refresh runs)
    # would keep showing "stale" even immediately after a refresh that just
    # fixed it, contradicting the success message rendered further down in
    # this exact same script run.
    if not configured_universe_tickers(engine):
        _staleness_banner.warning(NO_TRACKED_UNIVERSE_MESSAGE)
    elif is_universe_price_stale(engine, _stale_price_days):
        _staleness_banner.warning(
            f"Some tracked securities have price data older than the configured "
            f"{_stale_price_days}-day observation limit."
        )
    else:
        _staleness_banner.caption(f"Tracked universe's price data is within the configured {_stale_price_days}-day observation limit.")


if not st.session_state.get("auto_stale_refresh_attempted"):
    # Once per browser session, on first load: automatically ingest
    # whichever already-tracked tickers are actually stale, so a user
    # never has to notice the warning below and press Full Refresh
    # themselves just to see current data. Deliberately scoped to only
    # the stale subset (never the full universe) so this automatic
    # trigger's cost stays proportional to what is actually stale rather
    # than always paying for a full-universe refresh on every session
    # start -- the Full Refresh button below still does the full universe
    # for a deliberate, on-demand refresh. Guarded by session_state (set
    # unconditionally below, whether or not anything was actually stale)
    # so this never re-fires on a later rerun within the same session,
    # even if a subsequent auto-refresh attempt failed.
    st.session_state["auto_stale_refresh_attempted"] = True
    _stale_tickers = stale_universe_tickers(engine, _stale_price_days)
    if len(_stale_tickers) > MAX_AUTO_REFRESH_TICKERS:
        # Safety cap (see alpha_lab.refresh's own docstring on
        # MAX_AUTO_REFRESH_TICKERS): a stale count this large -- the whole
        # universe going stale at once, or a freshly-loaded large universe
        # -- would turn this trigger's "loading time doesn't increase
        # substantially" design goal into a multi-hour blocking page load
        # (one live provider round-trip per ticker, none of it
        # backgrounded). Skip the automatic attempt entirely; the warning
        # below and the manual Full Refresh button remain the way to
        # catch up on demand.
        st.warning(
            f"{len(_stale_tickers)} tickers are stale -- above the automatic "
            f"refresh's safety limit of {MAX_AUTO_REFRESH_TICKERS}, so it was "
            "skipped this session to avoid a very long page load. Use Full "
            "Refresh below to update the whole universe on demand."
        )
    elif _stale_tickers:
        with st.spinner(
            f"Automatically refreshing {len(_stale_tickers)} stale ticker(s) "
            f"(price/fundamental data), then rebuilding research..."
        ):
            try:
                _auto_status = run_research_refresh_guarded(
                    engine, settings, st.session_state, tickers=_stale_tickers
                )
            except Exception as _auto_error:  # noqa: BLE001 -- mirrors the Full Refresh
                # button's own handling below: never let an unexpected automatic-
                # refresh failure take the whole page down; existing research is
                # unaffected either way (the orchestrator's core refresh step
                # never corrupts it).
                st.warning(
                    f"Automatic refresh of stale data failed unexpectedly "
                    f"({_auto_error}); showing existing data."
                )
            else:
                if _auto_status is not None and _auto_status.core.research_error is None:
                    st.caption(
                        f"Automatically refreshed {_auto_status.core.tickers_succeeded}/"
                        f"{_auto_status.core.tickers_attempted} stale ticker(s) on session start "
                        f"(research state version {_auto_status.version_id[:12]})."
                    )
                    build_screener.clear()
                elif _auto_status is not None and _auto_status.core.research_error is not None:
                    st.warning(
                        f"Automatic refresh of stale data failed "
                        f"({_auto_status.core.research_error}); showing existing data."
                    )

_render_staleness_banner()
if st.button("🔄 Full Refresh (price + fundamental data + research)"):
    # Core only: price/fundamental ingestion + research rebuild -- never the
    # supplemental domains (Analyst Consensus/Technical/AI/News/Macro/
    # Donatien), which stay independently refreshable via their own pages/
    # scripts. See alpha_lab.refresh's module docstring for why this stays
    # synchronous and atomic rather than kicking off background work.
    with st.spinner("Refreshing core data — price/fundamental ingestion, then research rebuild..."):
        try:
            status = run_research_refresh_guarded(engine, settings, st.session_state, force_core=True)
        except Exception as error:  # noqa: BLE001 -- run_research_refresh_guarded only
            # guarantees a per-ticker provider failure or a rebuild failure land on
            # the returned status, never raised; an infrastructure-level failure
            # outside those paths is deliberately left to propagate (see
            # alpha_lab.research_refresh's module docstring). This button's own job
            # is to never take the whole page down for that, so it's caught here and
            # shown the same way any other refresh failure is -- existing research
            # is unaffected either way (the orchestrator's core refresh step never
            # corrupts it).
            unexpected_error = str(error)
            status = None
        else:
            unexpected_error = None
    if unexpected_error is not None:
        st.error(f"Full Refresh failed unexpectedly ({unexpected_error}); existing research is unchanged.")
    elif status is None:
        st.warning(
            "A refresh is already in progress (in this or another browser tab); "
            "wait for it to finish, then reload."
        )
    elif status.core.research_error is not None:
        st.error(
            f"Research rebuild failed ({status.core.research_error}); existing "
            f"research is unchanged. Ingested {status.core.tickers_succeeded}/"
            f"{status.core.tickers_attempted} ticker(s) ({status.core.tickers_failed} failed)."
        )
    elif status.tracked_universe_size == 0:
        st.warning(NO_TRACKED_UNIVERSE_MESSAGE)
    else:
        _tracked_total = status.tracked_universe_size
        if _tracked_total > MAX_FULL_UNIVERSE_REFRESH_BATCH:
            # See MAX_FULL_UNIVERSE_REFRESH_BATCH's own docstring: a universe
            # this large made one click a many-hour, feedback-free operation,
            # so run_core_refresh silently capped this call to a batch of the
            # stalest tickers instead of the whole universe. Say so explicitly
            # here -- otherwise "Ingested 200/200" on a 4000+-ticker universe
            # would read as a completed refresh rather than one batch of many.
            _remaining_stale = len(stale_universe_tickers(engine, _stale_price_days))
            st.success(
                f"Large universe ({_tracked_total} tracked securities) — refreshed a batch "
                f"of {status.core.tickers_succeeded}/{status.core.tickers_attempted} stale "
                f"ticker(s) ({status.core.tickers_failed} failed); research rebuilt for "
                f"{status.core.research_record_count} securit(y/ies). Research state "
                f"version {status.version_id[:12]}. "
                + (
                    f"{_remaining_stale} still stale — click Full Refresh again to continue "
                    "catching up."
                    if _remaining_stale
                    else "All tracked securities are now within the freshness window."
                )
            )
        else:
            st.success(
                f"Ingested {status.core.tickers_succeeded}/{status.core.tickers_attempted} "
                f"ticker(s) ({status.core.tickers_failed} failed); research rebuilt for "
                f"{status.core.research_record_count} securit(y/ies). "
                f"Research state version {status.version_id[:12]}."
            )
    if status is not None and (status.core.stopped_early or status.core.failure_reasons):
        _notes = []
        if status.core.stopped_early:
            _notes.append(
                f"Refresh {status.core.stopped_early}. {status.core.tickers_not_attempted} "
                "ticker(s) were not attempted."
            )
        if status.core.failure_reasons:
            _notes.append(
                "Failures: "
                + "; ".join(
                    f"{count}× {reason}"
                    for reason, count in sorted(
                        status.core.failure_reasons.items(), key=lambda item: -item[1]
                    )[:3]
                )
            )
        st.warning(" ".join(_notes))
    # Deliberately no st.rerun() here: Streamlit already runs this script
    # top-to-bottom on the click that got us here, and build_screener() is
    # called later in this SAME run (below) -- clearing its cache now is
    # enough for that call to pick up fresh data. An explicit rerun would
    # immediately restart the script, and since st.button() returns False
    # on that next run, the success/error message above would never be
    # re-emitted and Streamlit would silently drop it before the user
    # could read it.
    build_screener.clear()
    _render_staleness_banner()

_current_research_status = get_current_research_refresh_status(engine)
if _current_research_status is not None:
    with st.expander(
        f"Research state: version {_current_research_status.version_id[:12]} -- "
        f"{_current_research_status.tracked_universe_size} tracked securit(y/ies), "
        f"evaluated {_current_research_status.evaluation_date}"
    ):
        st.caption(
            "Read-only, pure-DB snapshot of what AlphaLab currently knows per evidence "
            "domain across the tracked universe -- never triggers a refresh itself. See "
            "alpha_lab.research_refresh's own module docstring."
        )
        st.dataframe(
            pd.DataFrame([
                {
                    "Domain": domain.label,
                    "Scope": domain.scope,
                    "Status": domain.status,
                    "Coverage": f"{domain.coverage:.0%}" if domain.coverage is not None else "—",
                    "Freshness": domain.freshness or "—",
                    "Detail": domain.detail or "—",
                }
                for domain in _current_research_status.domains
            ]),
            hide_index=True,
            width="stretch",
        )

st.header("Stock Screener")
screen = build_screener()
if screen.empty:
    st.info(
        "No persisted current research build exists. Run "
        "`python scripts/rebuild_research.py` after loading data."
    )
else:
    sectors = sorted(screen["Sector"].dropna().unique())
    selected = st.multiselect("Sector", sectors)
    minimum = st.slider("Minimum overall rating", 0, 100, 0)
    # pd.to_numeric first: when every row's Overall Rating is None (e.g. no
    # security has a computed score yet), the raw column's dtype is object,
    # and fillna(-1) on an object dtype triggers pandas' downcasting
    # deprecation warning -- coercing to float64 up front means fillna
    # never needs to change dtype, regardless of how many rows are None.
    overall_rating = pd.to_numeric(screen["Overall Rating"], errors="coerce")
    filtered = screen[overall_rating.fillna(-1) >= minimum]
    if selected:
        filtered = filtered[filtered["Sector"].isin(selected)]
    st.dataframe(filtered, width="stretch", hide_index=True, column_config={
        "Coverage": st.column_config.ProgressColumn("Coverage", min_value=0.0, max_value=1.0,
                                                     format="percent"),
    })
    ticker = st.selectbox("Factor breakdown", filtered["Ticker"] if not filtered.empty else screen["Ticker"])
    row = screen.set_index("Ticker").loc[ticker]
    factor_columns = ["Growth", "Revisions", "Quality", "Valuation", "Momentum",
                       "Financial Strength", "AI Rating", "Shareholder Return"]
    chart = pd.DataFrame({"Factor": factor_columns, "Score": [row[c] for c in factor_columns]}).dropna()
    if chart.empty:
        st.warning("Factor inputs are unavailable for this security.")
    else:
        st.plotly_chart(px.bar(chart, x="Factor", y="Score", range_y=[0, 100],
                               title=f"Why {ticker} received its score"))

st.header("Data Quality")
st.write("Unavailable fields remain blank and are excluded with visible coverage; no missing factor is silently converted to a positive signal.")
with Session(engine) as session:
    # One SQL MAX(date) per ticker, not every price row ever ingested --
    # the same fix (and the same reason) as alpha_lab.refresh.
    # stale_universe_tickers's own GROUP BY: this page reruns on every
    # widget interaction, so pulling the whole universe's full price
    # history into Python just to find each ticker's latest row is real,
    # avoidable cost that scales with total price rows (~500/ticker/year),
    # not with the universe size this table actually needs.
    latest_prices = session.execute(select(Price.ticker, func.max(Price.date)).group_by(Price.ticker)).all()
# Only the tracked universe: the price-freshness banner and Full Refresh
# both operate on it, so listing every catalog security or macro-proxy
# ticker that merely has a price row showed a wall of "stale" rows that no
# refresh could ever clear.
_tracked_for_quality = set(configured_universe_tickers(engine))
latest_by_ticker: dict[str, date] = {
    ticker: observed for ticker, observed in latest_prices if ticker in _tracked_for_quality
}
quality_rows = []
for ticker, observed in latest_by_ticker.items():
    issue = assess_freshness("price", observed, date.today(), settings.data_quality["stale_price_days"])
    quality_rows.append({"Ticker": ticker, "Latest price": observed, "Status": issue.status if issue else "ok",
                         "Detail": issue.detail if issue else "Current within configured limit"})
if quality_rows:
    st.dataframe(pd.DataFrame(quality_rows), hide_index=True, width="stretch")
st.caption("Unavailable evidence for any category (Growth, Revisions, Quality, Valuation, Momentum, Financial Strength, AI Rating, Shareholder Return) is shown as unavailable rather than imputed.")
