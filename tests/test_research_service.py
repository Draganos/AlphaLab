"""Focused tests for the read-only ResearchService boundary."""

from datetime import date

import pytest
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Security
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.research import ResearchService, StockResearch
from alpha_lab.screener import LiveResearchRecord


def _record(ticker: str, **updates) -> LiveResearchRecord:
    values = {
        "ticker": ticker,
        "company": f"{ticker} Inc",
        "price": 100.0,
        "market_cap": 1_000.0,
        "country": "US",
        "exchange": "NASDAQ",
        "sector": "Technology",
        "industry": "Software",
        "asset_type": "equity",
        "themes": [],
        "ethical_status": "PASS",
        "data_quality_status": "valid",
        "overall_score": 75.0,
        "overall_rank": 1,
        "category_scores": {},
        "category_coverage": {},
        "raw_metrics": {},
        "percentile_metrics": {},
        "overall_live_coverage": 0.75,
        "quantitative_coverage": 0.75,
        "ai_coverage": 0.0,
        "historical_coverage": 0.0,
        "confidence": "Strong",
        "provenance": {},
        "last_refreshed": None,
        "rating_version": "phase3-live-v2",
        "configuration_hash": "configuration-a",
        "evaluation_date": date(2026, 8, 30),
    }
    values.update(updates)
    return LiveResearchRecord.model_validate(values)


@pytest.fixture
def research_service(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'research-service.db'}")
    create_schema(engine)
    try:
        yield ResearchService(engine, load_settings()), engine
    finally:
        engine.dispose()


def test_get_stock_research_returns_none_when_no_current_build_exists(research_service):
    service, _ = research_service
    assert service.get_stock_research("AAPL") is None


def test_get_stock_research_returns_none_for_a_ticker_not_in_the_current_build(
    research_service,
):
    service, engine = research_service
    with Session(engine) as session:
        session.add(Security(ticker="AAPL"))
        session.commit()
    Phase3Repository(engine).save_current_research([_record("AAPL")])

    assert service.get_stock_research("MSFT") is None
    assert service.get_stock_research("AAPL") is not None


def test_get_stock_research_is_case_and_whitespace_insensitive(research_service):
    service, engine = research_service
    with Session(engine) as session:
        session.add(Security(ticker="AAPL"))
        session.commit()
    Phase3Repository(engine).save_current_research([_record("AAPL")])

    assert service.get_stock_research(" aapl ") is not None


def test_get_stock_research_returns_canonical_stock_research_via_build_stock_research(
    research_service,
):
    service, engine = research_service
    with Session(engine) as session:
        session.add(Security(ticker="AAPL"))
        session.commit()
    Phase3Repository(engine).save_current_research([_record("AAPL")])

    research = service.get_stock_research("AAPL")
    assert isinstance(research, StockResearch)
    assert research.ticker == "AAPL"


def test_get_stock_research_preserves_existing_score_and_coverage_unchanged(
    research_service,
):
    """The service must not recompute quantitative scores/coverage — it only
    converts the already-persisted LiveResearchRecord through
    build_stock_research."""
    service, engine = research_service
    with Session(engine) as session:
        session.add(Security(ticker="AAPL"))
        session.commit()
    record = _record("AAPL", overall_score=63.5, overall_live_coverage=0.42)
    Phase3Repository(engine).save_current_research([record])

    research = service.get_stock_research("AAPL")
    assert research.overall_score == pytest.approx(63.5)
    assert research.overall_coverage == pytest.approx(0.42)
    assert research.rating_version == record.rating_version
    assert research.configuration_hash == record.configuration_hash
    assert research.evaluation_date == record.evaluation_date


def test_list_current_research_returns_the_underlying_records_for_selection(
    research_service,
):
    service, engine = research_service
    with Session(engine) as session:
        session.add_all([Security(ticker="AAPL"), Security(ticker="MSFT")])
        session.commit()
    Phase3Repository(engine).save_current_research([_record("AAPL"), _record("MSFT")])

    records = service.list_current_research()
    assert {record.ticker for record in records} == {"AAPL", "MSFT"}
    assert all(isinstance(record, LiveResearchRecord) for record in records)


def test_get_stock_research_only_converts_the_requested_ticker_not_the_universe(
    research_service, monkeypatch
):
    """Guards against re-introducing a whole-universe conversion: only the
    matching record should ever reach build_stock_research."""
    service, engine = research_service
    with Session(engine) as session:
        session.add_all([Security(ticker="AAPL"), Security(ticker="MSFT")])
        session.commit()
    Phase3Repository(engine).save_current_research([_record("AAPL"), _record("MSFT")])

    converted_tickers = []
    import alpha_lab.research.service as service_module

    original = service_module.build_stock_research

    def _tracking_build(record):
        converted_tickers.append(record.ticker)
        return original(record)

    monkeypatch.setattr(service_module, "build_stock_research", _tracking_build)

    service.get_stock_research("AAPL")
    assert converted_tickers == ["AAPL"]
