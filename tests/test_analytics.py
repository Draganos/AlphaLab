import numpy as np
import pandas as pd
import pytest
from alpha_lab.analytics import performance_metrics


def test_metrics_known_drawdown_and_finite_edge_cases():
    index = pd.bdate_range("2024-01-01", periods=5)
    metrics = performance_metrics(pd.Series([100, 120, 90, 110, 115], index=index))
    assert metrics["total_return"] == pytest.approx(.15)
    assert metrics["max_drawdown"] == pytest.approx(-.25)
    assert metrics["sharpe"] is not None
    flat = performance_metrics(pd.Series([100, 100, 100], index=index[:3]))
    assert flat["sharpe"] is None
    assert flat["calmar"] is None


def test_alpha_beta_and_information_ratio():
    index = pd.bdate_range("2023-01-01", periods=260)
    benchmark_returns = np.tile([.01, -.005], 130)
    strategy_returns = .001 + 1.5 * benchmark_returns
    benchmark = pd.Series(100 * np.cumprod(1 + benchmark_returns), index=index)
    strategy = pd.Series(100 * np.cumprod(1 + strategy_returns), index=index)
    metrics = performance_metrics(strategy, benchmark)
    assert metrics["beta"] == pytest.approx(1.5, rel=.02)
    assert metrics["alpha"] is not None
    assert metrics["information_ratio"] is not None
