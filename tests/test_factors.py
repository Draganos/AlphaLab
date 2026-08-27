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


def test_factors_are_repeatable_and_respect_publication_date():
    evaluation = pd.Timestamp("2024-06-30")
    prices = pd.Series(np.linspace(10, 20, 300), index=pd.bdate_range(end=evaluation, periods=300))
    fundamentals = pd.DataFrame([
        {"period": "2023-03-31", "publication_date": "2023-05-10", "eps": 1.0, "revenue": 100},
        {"period": "2024-03-31", "publication_date": "2024-05-10", "eps": 2.0, "revenue": 120},
        {"period": "2024-06-30", "publication_date": "2024-08-10", "eps": 99.0, "revenue": 999},
    ])
    first = calculate_factors(prices, fundamentals, evaluation)
    second = calculate_factors(prices, fundamentals.sample(frac=1, random_state=7), evaluation)
    assert first.keys() == second.keys()
    for key in first:
        assert first[key] == second[key] or (np.isnan(first[key]) and np.isnan(second[key]))
    assert first["last_price"] == 20


def test_unknown_publication_date_is_unavailable_for_as_of_scoring():
    factors = calculate_factors(pd.Series([10.0], index=pd.to_datetime(["2024-01-01"])),
                                pd.DataFrame([{"period": "2023-12-31", "eps": 5.0}]), "2024-02-01")
    assert "eps_yoy_growth" not in factors
