import numpy as np
import pandas as pd
from alpha_lab.factors import calculate_factors, percentile_scores


def test_momentum_and_fundamental_factors():
    prices = pd.Series(np.arange(1, 301, dtype=float), index=pd.date_range("2023-01-01", periods=300))
    fundamentals = pd.DataFrame({"period": pd.date_range("2022-01-01", periods=5, freq="QE"), "eps": [1, 2, 3, 4, 2],
        "revenue": [100, 110, 120, 130, 150], "ebitda": [20]*5, "net_income": [10]*5,
        "free_cash_flow": [8]*5, "total_debt": [15]*5, "cash": [5]*5, "total_equity": [50]*5})
    result = calculate_factors(prices, fundamentals)
    assert result["return_1m"] > 0
    assert result["eps_yoy_growth"] == 1
    assert result["net_debt"] == 10


def test_missing_values_remain_missing():
    result = calculate_factors(pd.Series(dtype=float), pd.DataFrame())
    assert np.isnan(result["return_1m"])
    raw = pd.DataFrame({"quality": [1.0, np.nan, 3.0]}, index=list("ABC"))
    assert np.isnan(percentile_scores(raw).loc["B", "quality"])
