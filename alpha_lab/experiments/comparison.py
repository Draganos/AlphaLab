"""Equal-capital passive/manual/systematic experiment comparison."""

import pandas as pd
from alpha_lab.analytics import performance_metrics
from alpha_lab.backtest.engine import TransactionCostModel


def manual_buy_and_hold(prices: dict[str, pd.Series], weights: dict[str, float], initial_capital: float,
                        costs: TransactionCostModel | None = None) -> pd.Series:
    if any(weight < 0 for weight in weights.values()) or sum(weights.values()) > 1 + 1e-9:
        raise ValueError("Manual weights must be non-negative and cannot exceed 100%")
    model = costs or TransactionCostModel()
    curves = []
    residual_cash = initial_capital * (1 - sum(weights.values()))
    for ticker, weight in weights.items():
        series = prices[ticker].dropna().sort_index()
        allocation = initial_capital * weight
        execution_price = model.execution_price(float(series.iloc[0]), "BUY")
        quantity = max(0.0, (allocation - model.fixed_commission) /
                       (execution_price * (1 + model.percentage_commission)))
        spent = quantity * execution_price
        residual_cash += allocation - spent - model.commission(spent)
        curves.append((series * quantity).rename(ticker))
    joined = pd.concat(curves, axis=1).ffill().dropna() if curves else pd.DataFrame()
    return (joined.sum(axis=1) + residual_cash).rename("Manual")


def compare_experiments(curves: dict[str, pd.Series], risk_free_rate: float = 0.0) -> pd.DataFrame:
    rows = {}
    for name, curve in curves.items():
        metrics = performance_metrics(curve, risk_free_rate=risk_free_rate)
        rows[name] = {"Final value": float(curve.dropna().iloc[-1]), "CAGR": metrics["cagr"],
                      "Sharpe": metrics["sharpe"], "Max drawdown": metrics["max_drawdown"],
                      "Volatility": metrics["annualized_volatility"]}
    return pd.DataFrame(rows)
