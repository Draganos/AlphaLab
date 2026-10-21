"""The database-backed backtest runner shared by the CLI and the dashboard had
no test of its own: its price frames carried plain `date` objects, so every
run raised AttributeError before any trade."""

from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from alpha_lab.backtest.database_runner import load_price_frames, run_database_backtest
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security

START, END = date(2024, 1, 2), date(2024, 12, 30)


def _engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        for ticker, slope in (("A", 1.0), ("B", 0.6), ("C", 0.3), ("D", 0.1), ("BENCH", 0.4)):
            session.add(Security(ticker=ticker, sector="Test", is_tracked=True))
            day, offset = date(2023, 1, 2), 0
            while day <= END:
                if day.weekday() < 5:
                    value = 20 + slope * offset
                    session.add(Price(ticker=ticker, date=day, open=value, close=value, adjusted_close=value,
                                      volume=1_000_000, provider="test", ingested_at=datetime(2025, 1, 5, tzinfo=UTC)))
                    offset += 1
                day += timedelta(days=1)
        session.commit()
    return engine


def test_price_frames_have_a_datetime_index():
    engine = _engine()
    frames = load_price_frames(engine, ["A", "BENCH"], START, END)
    assert isinstance(frames["A"].index, pd.DatetimeIndex) and frames["A"].index[0].date() >= START
    engine.dispose()


def test_a_database_backtest_runs_end_to_end():
    engine = _engine()
    settings = load_settings()
    result, benchmark = run_database_backtest(
        engine, settings, ["A", "B", "C", "D"], START, END, benchmark="BENCH", min_score=0, minimum_coverage=0)
    assert len(result.nav) > 100 and len(benchmark) > 100
    assert result.rebalances and all(r.evaluation_date >= START for r in result.rebalances)
    assert "total_return" in result.metrics or result.metrics  # metrics are produced
    engine.dispose()
