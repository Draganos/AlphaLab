"""Passive/manual/AlphaLab experiment comparison page."""
from datetime import date, timedelta
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pandas as pd
import plotly.express as px
import streamlit as st
from alpha_lab.backtest.database_runner import load_price_frames, run_database_backtest
from alpha_lab.backtest import TransactionCostModel
from alpha_lab.config import load_settings
from alpha_lab.database import make_engine
from alpha_lab.experiments import compare_experiments, manual_buy_and_hold
from alpha_lab.research import load_universe

st.title("Experiments A / B / C")
st.write("Is AlphaLab outperforming a simple alternative after adjusting for risk and costs?")
settings = load_settings()
engine = make_engine(settings.database_url)
universe = load_universe("data/universes/us_research_sample.csv")
start = st.date_input("Start", date.today() - timedelta(days=365 * 3))
end = st.date_input("End", date.today())
benchmark = st.selectbox("Passive benchmark", ["SPY", "QQQ", "VT"])
manual_text = st.text_input("Manual weights (TICKER:weight)", "NVDA:0.5,MA:0.5")
if st.button("Compare equal-capital experiments", type="primary"):
    try:
        manual = {part.split(":")[0].strip().upper(): float(part.split(":")[1])
                  for part in manual_text.split(",") if part.strip()}
        result, passive = run_database_backtest(engine, settings, list(universe.tickers), start, end,
                                                benchmark=benchmark)
        frames = load_price_frames(engine, list(manual), start, end)
        manual_curve = manual_buy_and_hold({ticker: frame["close"] for ticker, frame in frames.items()},
                                           manual, settings.backtest.initial_capital,
                                           TransactionCostModel(**settings.backtest.costs.model_dump()))
        curves = {"Passive": passive, "Manual": manual_curve, "AlphaLab": result.nav}
        st.dataframe(compare_experiments(curves, settings.backtest.risk_free_rate), width="stretch")
        st.plotly_chart(px.line(pd.concat(curves, axis=1).dropna(), title="Equal-capital equity curves"), width="stretch")
        st.error(universe.limitation)
    except (ValueError, KeyError) as exc:
        st.warning(str(exc))
