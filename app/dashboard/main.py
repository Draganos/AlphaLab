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
    filtered = screen[(screen["Overall Rating"].fillna(-1) >= minimum)]
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
                               title=f"Why {ticker} received its score"), width="stretch")

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
