#!/usr/bin/env python
"""Deterministic offline Phase 2 backtest smoke test."""

from datetime import date
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
from alpha_lab.backtest import BacktestEngine
from alpha_lab.config import BacktestSettings, TransactionCostSettings
from alpha_lab.strategy.historical import HistoricalScore


def run() -> None:
    dates = pd.bdate_range("2024-01-01", periods=90)
    prices = {"SYNTH_A": pd.DataFrame({"open": np.linspace(10, 20, len(dates)),
                                       "close": np.linspace(10.1, 20.1, len(dates))}, index=dates),
              "SYNTH_B": pd.DataFrame({"open": np.linspace(20, 10, len(dates)),
                                       "close": np.linspace(19.9, 9.9, len(dates))}, index=dates)}

    def scores(as_of: date) -> list[HistoricalScore]:
        return [HistoricalScore("SYNTH_A", "Synthetic A", "Fixture", {"last_price": 1, "volatility": .1},
                                {}, {}, 90, .8, "Exceptional candidate", True, None,
                                "phase2-smoke", "fixture", as_of),
                HistoricalScore("SYNTH_B", "Synthetic B", "Fixture", {"last_price": 1, "volatility": .2},
                                {}, {}, 40, .8, "Weak", False, "score below threshold",
                                "phase2-smoke", "fixture", as_of)]

    settings = BacktestSettings(initial_capital=5000, costs=TransactionCostSettings(
        fixed_commission=1, percentage_commission=.001, spread=.001, slippage=.0005,
        minimum_trade_amount=10))
    result = BacktestEngine(prices, scores, settings, min_score=70, minimum_coverage=.7,
        min_positions=1, max_positions=2, max_position=1, max_sector=None).run(
            dates[0].date(), dates[-1].date())
    assert result.trades and all(trade.execution_date > trade.signal_date for trade in result.trades)
    assert result.total_transaction_costs > 0 and result.nav.iloc[-1] > 0
    assert (result.cash >= 0).all()
    print(f"AlphaLab Phase 2 smoke test passed: {len(result.trades)} trades, NAV={result.nav.iloc[-1]:.2f}")


if __name__ == "__main__":
    run()
