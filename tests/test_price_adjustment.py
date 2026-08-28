from datetime import date
import pandas as pd
import pytest

from alpha_lab.backtest.benchmark import buy_and_hold
from alpha_lab.backtest.database_runner import adjusted_price_values
from alpha_lab.database.models import Price


def test_split_adjusts_open_and_close_to_same_basis_and_preserves_nav():
    before = Price(ticker="SPLIT", date=date(2024, 1, 2), open=100, close=100,
                   adjusted_close=50, provider="test")
    after = Price(ticker="SPLIT", date=date(2024, 1, 3), open=50, close=50,
                  adjusted_close=50, provider="test")
    frame = pd.DataFrame([adjusted_price_values(before), adjusted_price_values(after)]).set_index("date")
    assert frame.iloc[0]["open"] == 50
    assert frame.iloc[0]["close"] == 50
    nav = buy_and_hold(frame, 1000)
    assert nav.iloc[0] == pytest.approx(1000)
    assert nav.iloc[1] == pytest.approx(1000)


@pytest.mark.parametrize(("raw_close", "adjusted_close", "expected_open"), [
    (0, 50, 100), (None, 50, 100), (100, None, 100), (float("nan"), 50, 100),
])
def test_adjustment_factor_handles_null_and_zero_safely(raw_close, adjusted_close, expected_open):
    price = Price(ticker="SAFE", date=date(2024, 1, 2), open=100, close=raw_close,
                  adjusted_close=adjusted_close, provider="test")
    assert adjusted_price_values(price)["open"] == expected_open
