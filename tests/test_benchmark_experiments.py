import pandas as pd
import pytest
from alpha_lab.backtest.benchmark import buy_and_hold
from alpha_lab.experiments import compare_experiments, manual_buy_and_hold


def test_benchmark_and_manual_start_with_equal_capital():
    index = pd.bdate_range("2024-01-01", periods=5)
    frame = pd.DataFrame({"open": [10] * 5, "close": [10, 11, 12, 13, 14]}, index=index)
    passive = buy_and_hold(frame, 5000)
    manual = manual_buy_and_hold({"A": frame["close"]}, {"A": 1}, 5000)
    assert passive.iloc[0] == pytest.approx(5000)
    assert manual.iloc[0] == pytest.approx(5000)
    table = compare_experiments({"Passive": passive, "Manual": manual})
    assert list(table.columns) == ["Passive", "Manual"]
