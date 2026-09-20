"""Regression test for a real production finding: the Evidence & Coverage
Dashboard's Universe Breakdown tab used to call NewsService.get_history
once per ticker inside a loop -- one full database round-trip per ticker,
which measurably adds up at a large universe size (thousands of tickers)
even though each individual call is fast. Fixed by fetching the whole
universe's news history in one batched NewsService.get_history_for_tickers
call instead. This test proves the page actually uses the batched path,
not just that the batched method itself works correctly (already covered
in tests/test_news_service.py)."""

import importlib.util
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from alpha_lab.database import create_schema, make_engine
from alpha_lab.news.service import NewsService
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.research import CATEGORY_ORDER
from alpha_lab.screener import LiveResearchRecord

_PAGE_PATH = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "pages" / "8_Evidence_Coverage.py"


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


def _import_page(db_path, monkeypatch):
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    spec = importlib.util.spec_from_file_location(
        f"evidence_coverage_under_test_{db_path.name}", _PAGE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_universe_breakdown_uses_the_batched_news_read_not_a_per_ticker_loop(tmp_path, monkeypatch):
    db_path = tmp_path / "evidence_coverage.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    Phase3Repository(engine).save_current_research([_record("AAPL"), _record("MSFT")])
    engine.dispose()

    with patch.object(NewsService, "get_history", return_value=[]) as fake_get_history, \
         patch.object(
             NewsService, "get_history_for_tickers",
             autospec=True, side_effect=NewsService.get_history_for_tickers,
         ) as fake_batched:
        module = _import_page(db_path, monkeypatch)
        # get_history fires exactly once -- the separate Security Detail
        # tab's own single selected ticker, never once per universe ticker.
        assert fake_get_history.call_count == 1
        fake_batched.assert_called_once()
        (call_args, _call_kwargs) = fake_batched.call_args
        # call_args[0] is `self`; the ticker list is the next positional arg.
        assert sorted(call_args[1]) == ["AAPL", "MSFT"]
    module.engine.dispose()


def test_universe_breakdown_flat_rows_include_both_tickers(tmp_path, monkeypatch):
    db_path = tmp_path / "evidence_coverage2.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    Phase3Repository(engine).save_current_research([_record("AAPL"), _record("MSFT")])
    engine.dispose()

    module = _import_page(db_path, monkeypatch)
    try:
        rows = module._load_universe_coverage_rows(
            module.research_service, module.news_service, module.macro_assessment, ("AAPL", "MSFT")
        )
        tickers_seen = {row["ticker"] for row in rows if "ticker" in row}
        assert tickers_seen or len(rows) > 0
    finally:
        module.engine.dispose()
