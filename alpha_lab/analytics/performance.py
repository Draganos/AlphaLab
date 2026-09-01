"""Finite-safe performance analytics for strategy and benchmark curves."""

import math
import numpy as np
import pandas as pd


def performance_metrics(nav: pd.Series, benchmark: pd.Series | None = None,
                        risk_free_rate: float = 0.0, periods_per_year: int = 252) -> dict[str, float | None]:
    values = nav.dropna().astype(float).sort_index()
    if len(values) < 2 or values.iloc[0] <= 0:
        return {name: None for name in _METRIC_NAMES}
    returns = values.pct_change().dropna()
    years = _years(values.index, len(returns), periods_per_year)
    total = values.iloc[-1] / values.iloc[0] - 1
    cagr = (values.iloc[-1] / values.iloc[0]) ** (1 / years) - 1 if years > 0 else None
    volatility = _finite(returns.std(ddof=1) * math.sqrt(periods_per_year)) if len(returns) > 1 else None
    excess = returns - risk_free_rate / periods_per_year
    sharpe = _ratio(excess.mean() * periods_per_year, volatility)
    downside = returns[returns < 0]
    downside_deviation = (downside.std(ddof=1) * math.sqrt(periods_per_year)
                          if len(downside) > 1 else None)
    sortino = _ratio(excess.mean() * periods_per_year, downside_deviation)
    drawdown = values / values.cummax() - 1
    max_drawdown = float(drawdown.min())
    calmar = _ratio(cagr, abs(max_drawdown))
    beta = alpha = information = None
    if benchmark is not None:
        joined = pd.concat([returns.rename("strategy"), benchmark.pct_change().rename("benchmark")], axis=1).dropna()
        if len(joined) > 1:
            benchmark_variance = joined["benchmark"].var(ddof=1)
            if benchmark_variance > 0:
                beta = _finite(joined.cov().loc["strategy", "benchmark"] / benchmark_variance)
                alpha = _finite((joined["strategy"].mean() - risk_free_rate / periods_per_year
                                 - beta * (joined["benchmark"].mean() - risk_free_rate / periods_per_year))
                                * periods_per_year)
            active = joined["strategy"] - joined["benchmark"]
            tracking = active.std(ddof=1) * math.sqrt(periods_per_year)
            information = _ratio(active.mean() * periods_per_year, tracking)
    return {"total_return": float(total), "cagr": _finite(cagr), "annualized_volatility": volatility,
            "sharpe": sharpe, "sortino": sortino, "max_drawdown": max_drawdown, "calmar": calmar,
            "alpha": alpha, "beta": beta, "information_ratio": information}


_METRIC_NAMES = ("total_return", "cagr", "annualized_volatility", "sharpe", "sortino",
                 "max_drawdown", "calmar", "alpha", "beta", "information_ratio")


def _years(index: pd.Index, observations: int, periods_per_year: int) -> float:
    if isinstance(index, pd.DatetimeIndex) and len(index) > 1:
        return max((index[-1] - index[0]).days / 365.25, 1 / periods_per_year)
    return observations / periods_per_year


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or not math.isfinite(denominator) or denominator <= 0:
        return None
    return _finite(numerator / denominator)


def _finite(value: float | None) -> float | None:
    return float(value) if value is not None and np.isfinite(value) else None
