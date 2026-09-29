"""Regression test for a real production finding: the main dashboard's
Data Quality table used to select every Price row (ticker, date) for the
whole universe, ordered by ticker/date, and pick each ticker's latest row
in Python -- a full-table read that scales with total price rows
(~500/ticker/year), on every rerun of a page Streamlit re-executes on
every widget interaction. Fixed with the same SQL `MAX(date) GROUP BY
ticker` pattern alpha_lab.refresh.stale_universe_tickers already
established. This test proves the fix is still correct: with multiple
Price rows per ticker inserted out of date order, the table must still
report each ticker's true latest date, not merely *a* date."""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.research import CATEGORY_ORDER
from alpha_lab.screener import LiveResearchRecord

_MAIN_PATH = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "main.py"


def _record(ticker: str) -> LiveResearchRecord:
    return LiveResearchRecord(
        ticker=ticker, company=f"{ticker} Inc", price=100.0, market_cap=1_000.0,
        country="US", exchange="NASDAQ", sector="Technology", industry="Software",
        asset_type="equity", themes=[], ethical_status="PASS", data_quality_status="valid",
        overall_score=70.0, overall_rank=1,
        category_scores={name: None for name in CATEGORY_ORDER},
        category_coverage={name: 0.0 for name in CATEGORY_ORDER},
        raw_metrics={}, percentile_metrics={}, overall_live_coverage=0.5,
        quantitative_coverage=0.5, ai_coverage=0.0, historical_coverage=0.0,
        confidence="Moderate", provenance={}, last_refreshed=None,
        rating_version="test-v1", configuration_hash="test-config",
        evaluation_date=date.today(),
    )


def _import_main(db_path, monkeypatch):
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    spec = importlib.util.spec_from_file_location(
        f"dashboard_main_data_quality_{db_path.name}", _MAIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_data_quality_reports_each_tickers_true_latest_date_not_just_any_row(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    today = date.today()
    with Session(engine) as session:
        session.add(Security(ticker="AAPL", country="US", currency="USD", is_tracked=True))
        session.add(Security(ticker="MSFT", country="US", currency="USD", is_tracked=True))
        # Inserted out of date order, and with several rows per ticker --
        # the old code's ORDER BY ticker, date DESC relied on insertion
        # producing a sorted result; a GROUP BY MAX must be correct
        # regardless of insertion order.
        for offset in (10, 0, 5):
            session.add(Price(
                ticker="AAPL", date=today - timedelta(days=offset), close=100.0,
                high=101.0, low=99.0, provider="fixture", currency="USD", source="test",
            ))
        for offset in (3, 20, 1):
            session.add(Price(
                ticker="MSFT", date=today - timedelta(days=offset), close=200.0,
                high=201.0, low=199.0, provider="fixture", currency="USD", source="test",
            ))
        session.commit()
    Phase3Repository(engine).save_current_research([_record("AAPL"), _record("MSFT")])
    engine.dispose()

    module = _import_main(db_path, monkeypatch)
    try:
        assert module.latest_by_ticker["AAPL"] == today - timedelta(days=0)
        assert module.latest_by_ticker["MSFT"] == today - timedelta(days=1)
    finally:
        module.engine.dispose()


def test_data_quality_lists_only_tracked_securities(tmp_path, monkeypatch):
    """Regression: the table listed every ticker with any price row --
    thousands of untracked catalog securities and macro-proxy tickers --
    as "stale", a wall of rows that Full Refresh (tracked universe only)
    could never clear."""
    db_path = tmp_path / "dashboard.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    old = date.today() - timedelta(days=40)
    with Session(engine) as session:
        session.add(Security(ticker="MSFT", country="US", currency="USD", is_tracked=True))
        session.add(Security(ticker="ELF", country="US", currency="USD", is_tracked=False))
        session.add(Security(ticker="^VIX", country="US", currency="USD", is_tracked=False))
        for ticker in ("MSFT", "ELF", "^VIX"):
            session.add(Price(
                ticker=ticker, date=old, close=10.0, high=11.0, low=9.0,
                provider="fixture", currency="USD", source="test",
            ))
        session.commit()
    engine.dispose()

    module = _import_main(db_path, monkeypatch)
    try:
        assert set(module.latest_by_ticker) == {"MSFT"}
    finally:
        module.engine.dispose()
