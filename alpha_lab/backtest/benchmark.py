"""Cost-consistent passive buy-and-hold benchmark curves."""

import pandas as pd
from alpha_lab.backtest.engine import TransactionCostModel

SUPPORTED_BENCHMARKS = ("SPY", "QQQ", "VT")


def buy_and_hold(prices: pd.DataFrame, initial_capital: float,
                 costs: TransactionCostModel | None = None) -> pd.Series:
    frame = prices.dropna(subset=["open", "close"]).sort_index()
    if frame.empty:
        raise ValueError("Benchmark has no compatible observations")
    model = costs or TransactionCostModel()
    execution_price = model.execution_price(float(frame.iloc[0]["open"]), "BUY")
    commission_rate = model.percentage_commission
    quantity = max(0.0, (initial_capital - model.fixed_commission) /
                   (execution_price * (1 + commission_rate)))
    commission = model.commission(quantity * execution_price)
    cash = initial_capital - quantity * execution_price - commission
    return (cash + quantity * frame["close"]).rename("benchmark_nav")
