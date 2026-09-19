"""AlphaLab Phase 1 Streamlit research dashboard."""

from pathlib import Path
import sys
from datetime import date
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database.models import Price
from alpha_lab.database.session import create_schema, make_engine
from alpha_lab.data_quality import assess_freshness
from alpha_lab.refresh import is_universe_price_stale, run_core_refresh_guarded, stale_universe_tickers
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


st.title("α AlphaLab")
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


def _render_staleness_banner() -> None:
    # A placeholder rather than a plain st.warning/st.caption call so this
    # can be re-rendered in place after a same-run Full Refresh below --
    # otherwise this banner (computed before the button's own refresh runs)
    # would keep showing "stale" even immediately after a refresh that just
    # fixed it, contradicting the success message rendered further down in
    # this exact same script run.
    if is_universe_price_stale(engine, _stale_price_days):
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
    if _stale_tickers:
        with st.spinner(
            f"Automatically refreshing {len(_stale_tickers)} stale ticker(s) "
            f"(price/fundamental data), then rebuilding research..."
        ):
            try:
                _auto_result = run_core_refresh_guarded(
                    engine, settings, st.session_state, tickers=_stale_tickers
                )
            except Exception as _auto_error:  # noqa: BLE001 -- mirrors the Full Refresh
                # button's own handling below: never let an unexpected automatic-
                # refresh failure take the whole page down; existing research is
                # unaffected either way (run_core_refresh never corrupts it).
                st.warning(
                    f"Automatic refresh of stale data failed unexpectedly "
                    f"({_auto_error}); showing existing data."
                )
            else:
                if _auto_result is not None and _auto_result.ok:
                    st.caption(
                        f"Automatically refreshed {len(_auto_result.tickers_succeeded)}/"
                        f"{len(_auto_result.tickers_attempted)} stale ticker(s) on session start."
                    )
                    build_screener.clear()
                elif _auto_result is not None and not _auto_result.ok:
                    st.warning(
                        f"Automatic refresh of stale data failed "
                        f"({_auto_result.research_error}); showing existing data."
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
            result = run_core_refresh_guarded(engine, settings, st.session_state)
        except Exception as error:  # noqa: BLE001 -- run_core_refresh_guarded only
            # guarantees a per-ticker provider failure or a rebuild failure land on
            # the returned CoreRefreshResult, never raised; an infrastructure-level
            # failure outside those paths is deliberately left to propagate (see
            # alpha_lab.refresh's module docstring). This button's own job is to
            # never take the whole page down for that, so it's caught here and
            # shown the same way any other refresh failure is -- existing research
            # is unaffected either way (run_core_refresh never corrupts it).
            unexpected_error = str(error)
            result = None
        else:
            unexpected_error = None
    if unexpected_error is not None:
        st.error(f"Full Refresh failed unexpectedly ({unexpected_error}); existing research is unchanged.")
    elif result is None:
        st.warning("A refresh is already in progress for this session.")
    elif not result.ok:
        st.error(
            f"Research rebuild failed ({result.research_error}); existing "
            f"research is unchanged. Ingested {len(result.tickers_succeeded)}/"
            f"{len(result.tickers_attempted)} ticker(s) ({len(result.tickers_failed)} failed)."
        )
    else:
        st.success(
            f"Ingested {len(result.tickers_succeeded)}/{len(result.tickers_attempted)} "
            f"ticker(s) ({len(result.tickers_failed)} failed); research rebuilt for "
            f"{result.research_record_count} securit(y/ies)."
        )
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
    latest_prices = session.execute(select(Price.ticker, Price.date).order_by(Price.ticker, Price.date.desc())).all()
latest_by_ticker: dict[str, date] = {}
for ticker, observed in latest_prices:
    latest_by_ticker.setdefault(ticker, observed)
quality_rows = []
for ticker, observed in latest_by_ticker.items():
    issue = assess_freshness("price", observed, date.today(), settings.data_quality["stale_price_days"])
    quality_rows.append({"Ticker": ticker, "Latest price": observed, "Status": issue.status if issue else "ok",
                         "Detail": issue.detail if issue else "Current within configured limit"})
if quality_rows:
    st.dataframe(pd.DataFrame(quality_rows), hide_index=True, width="stretch")
st.caption("Unavailable evidence for any category (Growth, Revisions, Quality, Valuation, Momentum, Financial Strength, AI Rating, Shareholder Return) is shown as unavailable rather than imputed.")
