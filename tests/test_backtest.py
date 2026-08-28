from datetime import date
import pandas as pd
import pytest

from alpha_lab.backtest import BacktestEngine
from alpha_lab.config import BacktestSettings, TransactionCostSettings
from alpha_lab.strategy.historical import HistoricalScore


def _score(as_of: date, coverage: float = 1) -> list[HistoricalScore]:
    return [HistoricalScore("A", "A", "Tech", {"last_price": 1, "volatility": .1}, {}, {},
        90, coverage, "Strong", True, None, "test", "hash", as_of)]


def _prices() -> dict[str, pd.DataFrame]:
    index = pd.to_datetime(["2024-01-30", "2024-01-31", "2024-02-02", "2024-02-05", "2024-02-29", "2024-03-01"])
    return {"A": pd.DataFrame({"open": [10, 10, 11, 12, 13, 14],
                                "close": [10, 10.5, 11.5, 12.5, 13.5, 14.5]}, index=index)}


def _settings(cost=.0) -> BacktestSettings:
    return BacktestSettings(initial_capital=1000, fractional_shares=True,
        costs=TransactionCostSettings(percentage_commission=cost, spread=0, slippage=0,
                                      fixed_commission=0, minimum_trade_amount=0))


def _run(cost=0, coverage=1):
    engine = BacktestEngine(_prices(), lambda day: _score(day, coverage), _settings(cost),
        min_score=70, minimum_coverage=.7, min_positions=1, max_positions=1,
        max_position=1, max_sector=None)
    return engine.run(date(2024, 1, 30), date(2024, 3, 1))


def test_signal_executes_at_next_available_open_not_same_close():
    result = _run()
    trade = result.trades[0]
    assert trade.signal_date == date(2024, 1, 31)
    assert trade.execution_date == date(2024, 2, 2)  # missing Feb 1 / weekend handled by calendar
    assert trade.execution_price == 11


def test_low_coverage_stays_in_cash_with_audited_reason():
    result = _run(coverage=.15)
    assert result.trades == []
    assert (result.cash == 1000).all()
    assert result.rebalances[0].excluded["A"] == "insufficient coverage"


def test_accounting_reconciles_and_costs_reduce_return():
    gross, net = _run(0), _run(.01)
    assert net.nav.iloc[-1] < gross.nav.iloc[-1]
    assert net.total_transaction_costs > 0
    assert (net.cash >= -1e-9).all()
    final_value = net.cash.iloc[-1] + net.holdings["A"] * _prices()["A"].iloc[-1]["close"]
    assert net.nav.iloc[-1] == pytest.approx(final_value)


def test_each_security_executes_on_its_own_next_open_and_missing_open_never_zeros_nav():
    dates_a = pd.to_datetime(["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-05"])
    dates_b = pd.to_datetime(["2024-01-30", "2024-01-31", "2024-02-02", "2024-02-05"])
    prices = {
        "A": pd.DataFrame({"open": [50, 50, 50, 50], "close": [50, 50, 50, 50]}, index=dates_a),
        "B": pd.DataFrame({"open": [100, 100, 100, 100], "close": [100, 100, 100, 100]}, index=dates_b),
    }

    def scores(day):
        return [HistoricalScore(ticker, ticker, "Test", {"last_price": 1, "volatility": .1}, {}, {},
            90, 1, "Strong", True, None, "test", "hash", day) for ticker in ("A", "B")]

    result = BacktestEngine(prices, scores, _settings(), min_score=0, minimum_coverage=0,
        min_positions=2, max_positions=2, max_position=.5, max_sector=None).run(
            date(2024, 1, 30), date(2024, 2, 5))
    buys = {trade.ticker: trade.execution_date for trade in result.trades if trade.side == "BUY"}
    assert buys == {"A": date(2024, 2, 1), "B": date(2024, 2, 2)}
    assert (result.nav == 1000).all()
    assert all(snapshot.total_nav == pytest.approx(snapshot.cash + snapshot.market_value)
               for snapshot in result.snapshots)
