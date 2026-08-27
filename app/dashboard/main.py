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
from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.database.session import make_engine
from alpha_lab.data_quality import assess_freshness
from alpha_lab.factors import calculate_factors, percentile_scores
from alpha_lab.strategy import composite_score, interpretation

st.set_page_config(page_title="AlphaLab", page_icon="α", layout="wide")
settings = load_settings()
engine = make_engine(settings.database_url)


@st.cache_data(ttl=60)
def build_screener() -> pd.DataFrame:
    with Session(engine) as session:
        securities = session.scalars(select(Security)).all()
        raw: dict[str, dict[str, float]] = {}
        metadata: dict[str, Security] = {}
        for security in securities:
            prices = session.scalars(select(Price).where(Price.ticker == security.ticker).order_by(Price.date)).all()
            fundamentals = session.scalars(select(Fundamental).where(Fundamental.ticker == security.ticker).order_by(Fundamental.period)).all()
            price_series = pd.Series({p.date: p.adjusted_close or p.close for p in prices}, dtype=float)
            fund_frame = pd.DataFrame([{column: getattr(f, column) for column in ["period", "revenue", "ebitda", "net_income", "eps", "free_cash_flow", "total_debt", "cash", "total_equity"]} for f in fundamentals])
            raw[security.ticker] = calculate_factors(price_series, fund_frame, date.today())
            metadata[security.ticker] = security
    if not raw:
        return pd.DataFrame()
    raw_frame = pd.DataFrame.from_dict(raw, orient="index")
    scored = percentile_scores(raw_frame)
    rows = []
    for ticker in scored.index:
        categories = {
            "earnings": scored.at[ticker, "eps_yoy_growth"] if "eps_yoy_growth" in scored else None,
            "revisions": None,
            "fundamentals": scored.loc[ticker, [c for c in ["revenue_yoy_growth", "ebitda_margin", "net_margin", "roe"] if c in scored]].mean(skipna=True),
            "valuation": None,
            "momentum": scored.loc[ticker, [c for c in ["return_3m", "return_6m", "momentum_12_1", "distance_ma200"] if c in scored]].mean(skipna=True),
            "balance_sheet": scored.loc[ticker, [c for c in ["debt_to_ebitda"] if c in scored]].mean(skipna=True),
            "ai": None, "dividend": None,
        }
        categories = {k: (None if pd.isna(v) else float(v)) for k, v in categories.items()}
        result = composite_score(categories, settings.weights, date.today())
        security = metadata[ticker]
        rows.append({"Ticker": ticker, "Company": security.company_name, "Sector": security.sector,
                     "Price": raw_frame.at[ticker, "last_price"] if "last_price" in raw_frame else None,
                     "Composite score": result.score, "Interpretation": interpretation(result.score),
                     "Data coverage": result.coverage, "EPS score": categories["earnings"],
                     "Revision score": None, "Valuation score": None, "Momentum score": categories["momentum"],
                     "Quality score": categories["fundamentals"], "Balance sheet score": categories["balance_sheet"],
                     "Dividend score": None, "AI score": None})
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
    st.warning("No securities are loaded. Run `python scripts/load_us_data.py` with network access. AlphaLab will not fabricate sample prices.")
else:
    sectors = sorted(screen["Sector"].dropna().unique())
    selected = st.multiselect("Sector", sectors)
    minimum = st.slider("Minimum composite score", 0, 100, 0)
    filtered = screen[(screen["Composite score"].fillna(-1) >= minimum)]
    if selected:
        filtered = filtered[filtered["Sector"].isin(selected)]
    st.dataframe(filtered, width="stretch", hide_index=True)
    ticker = st.selectbox("Factor breakdown", filtered["Ticker"] if not filtered.empty else screen["Ticker"])
    row = screen.set_index("Ticker").loc[ticker]
    factor_columns = [c for c in screen if c.endswith("score") and c != "Composite score"]
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
st.caption("Estimate revisions, valuation, dividend, and AI inputs are marked unavailable/unsupported in Phase 1 rather than imputed.")
