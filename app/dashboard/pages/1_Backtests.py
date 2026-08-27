"""Point-in-time AlphaLab backtest page."""
from datetime import date, timedelta
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pandas as pd
import plotly.express as px
import streamlit as st
from alpha_lab.analytics import performance_metrics
from alpha_lab.backtest.database_runner import run_database_backtest
from alpha_lab.config import load_settings
from alpha_lab.database import make_engine
from alpha_lab.research import load_universe

st.title("Backtests")
st.error("SURVIVORSHIP BIAS RISK — the sample universe is not historical constituent membership.")
st.info("Point-in-time rule: signals use closes and published fundamentals through T; execution is next available open.")
settings = load_settings()
engine = make_engine(settings.database_url)
universe = load_universe("data/universes/us_research_sample.csv")
c1, c2, c3 = st.columns(3)
start = c1.date_input("Start", date.today() - timedelta(days=365 * 3))
end = c2.date_input("End", date.today())
capital = c3.number_input("Initial capital", min_value=100.0, value=float(settings.backtest.initial_capital))
benchmark = c1.selectbox("Benchmark", ["SPY", "QQQ", "VT"])
weighting = c2.selectbox("Weighting", ["equal", "score", "inverse_volatility"])
rebalance = c3.selectbox("Rebalance", ["monthly", "quarterly"])
min_score = c1.number_input("Minimum score", 0.0, 100.0, float(settings.strategy.min_score))
coverage = c2.number_input("Minimum coverage", 0.0, 1.0, float(settings.strategy.minimum_data_coverage))
if st.button("Run point-in-time backtest", type="primary"):
    configured = settings.model_copy(deep=True)
    configured.backtest.initial_capital = capital
    configured.backtest.rebalance = rebalance
    try:
        result, benchmark_nav = run_database_backtest(engine, configured, list(universe.tickers), start, end,
            benchmark=benchmark, weighting=weighting, min_score=min_score, minimum_coverage=coverage)
        curves = pd.concat([result.nav, benchmark_nav], axis=1).dropna()
        st.plotly_chart(px.line(curves, title="Strategy vs benchmark NAV"), width="stretch")
        drawdowns = curves / curves.cummax() - 1
        st.plotly_chart(px.line(drawdowns, title="Drawdown"), width="stretch")
        st.subheader("Strategy metrics")
        st.dataframe(pd.DataFrame([result.metrics]), width="stretch")
        st.subheader("Benchmark metrics")
        st.dataframe(pd.DataFrame([performance_metrics(benchmark_nav)]), width="stretch")
        st.metric("Transaction costs", f"{result.total_transaction_costs:,.2f}")
        st.metric("Turnover", f"{result.turnover:.2%}")
        st.plotly_chart(px.line((result.cash / result.nav).rename("cash allocation")), width="stretch")
        st.subheader("Trades")
        st.dataframe(pd.DataFrame([vars(item) for item in result.trades]), width="stretch")
        st.subheader("Rebalance decisions")
        st.dataframe(pd.DataFrame([vars(item) for item in result.rebalances]), width="stretch")
    except ValueError as exc:
        st.warning(str(exc))
st.caption("Missing publication timestamps remain unavailable. Real yfinance histories may therefore be mostly momentum-driven.")
