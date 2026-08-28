"""Database-backed orchestration shared by CLI and Streamlit."""

from datetime import date
import math
import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.backtest.benchmark import buy_and_hold
from alpha_lab.backtest.engine import BacktestEngine, BacktestResult, TransactionCostModel
from alpha_lab.config import Settings
from alpha_lab.database.models import Price
from alpha_lab.strategy import HistoricalScoringService


def adjusted_price_values(price: Price) -> dict[str, object]:
    """Return a split-consistent open/close pair from a raw provider observation."""
    raw_close = price.close
    adjusted_close = price.adjusted_close
    factor = adjusted_close / raw_close if _valid(adjusted_close) and _valid(raw_close) and raw_close != 0 else 1.0
    factor = factor if math.isfinite(factor) and factor > 0 else 1.0
    adjusted_open = price.open * factor if _valid(price.open) else None
    valuation_close = adjusted_close if _valid(adjusted_close) else raw_close
    return {"date": price.date, "open": adjusted_open, "close": valuation_close, "volume": price.volume}


def _valid(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def load_price_frames(engine: Engine, tickers: list[str] | tuple[str, ...], start: date, end: date) -> dict[str, pd.DataFrame]:
    frames = {}
    with Session(engine) as session:
        for ticker in tickers:
            rows = session.scalars(select(Price).where(
                Price.ticker == ticker, Price.date >= start, Price.date <= end).order_by(Price.date)).all()
            if rows:
                frames[ticker] = pd.DataFrame([adjusted_price_values(row) for row in rows]).set_index("date")
    return frames


def run_database_backtest(engine: Engine, settings: Settings, tickers: list[str], start: date, end: date,
                          *, benchmark: str = "SPY", weighting: str | None = None,
                          min_score: float | None = None, minimum_coverage: float | None = None
                          ) -> tuple[BacktestResult, pd.Series]:
    all_tickers = list(dict.fromkeys([*tickers, benchmark]))
    frames = load_price_frames(engine, all_tickers, start, end)
    if benchmark not in frames:
        raise ValueError(f"Benchmark {benchmark} has no price data in the requested period")
    benchmark_curve = buy_and_hold(frames[benchmark], settings.backtest.initial_capital,
                                   TransactionCostModel(**settings.backtest.costs.model_dump()))
    effective_start = max(start, benchmark_curve.index[0].date())
    research_frames = {ticker: frame for ticker, frame in frames.items() if ticker in tickers}
    scorer = HistoricalScoringService(engine, settings)
    score_floor = settings.strategy.min_score if min_score is None else min_score
    coverage_floor = (settings.strategy.minimum_data_coverage if minimum_coverage is None
                      else minimum_coverage)
    engine_runner = BacktestEngine(research_frames,
        lambda as_of: scorer.score_universe_as_of(as_of, tickers, min_score=score_floor,
                                                   minimum_coverage=coverage_floor),
        settings.backtest, min_score=score_floor, minimum_coverage=coverage_floor,
        min_positions=settings.strategy.min_positions, max_positions=settings.strategy.max_positions,
        max_position=settings.risk.max_position, max_sector=settings.risk.max_sector, weighting=weighting)
    result = engine_runner.run(effective_start, end, benchmark_curve)
    return result, benchmark_curve
