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


# --- Historical snapshot boundary --------------------------------------


def _seed_current_research(engine, ticker: str = "AAPL", **record_overrides) -> None:
    with Session(engine) as session:
        if session.get(Security, ticker) is None:
            session.add(Security(ticker=ticker))
            session.commit()
    Phase3Repository(engine).save_current_research([_record(ticker, **record_overrides)])


def test_persist_snapshot_then_get_research_snapshot_round_trips(research_service):
    service, engine = research_service
    _seed_current_research(engine)
    research = service.get_stock_research("AAPL")

    summary = service.persist_snapshot(research)
    reloaded = service.get_research_snapshot(summary.snapshot_id)

    assert reloaded is not None
    assert reloaded.ticker == "AAPL"
    assert reloaded.overall_score == research.overall_score


def test_get_research_snapshot_for_unknown_id_returns_none(research_service):
    service, _ = research_service
    assert service.get_research_snapshot("unknown-id") is None


def test_get_latest_snapshot_is_distinct_from_get_stock_research(research_service):
    """get_latest_snapshot reflects the last *persisted* history entry, which
    can lag behind current research if nothing has been saved since a
    refresh — the two accessors must not be conflated."""
    service, engine = research_service
    _seed_current_research(engine, overall_score=50.0)

    # No snapshot persisted yet: current research exists, history does not.
    assert service.get_stock_research("AAPL") is not None
    assert service.get_latest_snapshot("AAPL") is None

    first_research = service.get_stock_research("AAPL")
    service.persist_snapshot(first_research)
    assert service.get_latest_snapshot("AAPL").overall_score == pytest.approx(50.0)

    # Current research changes, but nothing is re-persisted.
    Phase3Repository(engine).save_current_research([_record("AAPL", overall_score=90.0)])
    assert service.get_stock_research("AAPL").overall_score == pytest.approx(90.0)
    assert service.get_latest_snapshot("AAPL").overall_score == pytest.approx(50.0)


def test_get_research_history_lists_persisted_snapshots_newest_first(research_service):
    service, engine = research_service
    _seed_current_research(engine, evaluation_date=date(2026, 8, 28), overall_score=60.0)
    service.persist_snapshot(service.get_stock_research("AAPL"))

    Phase3Repository(engine).save_current_research(
        [_record("AAPL", evaluation_date=date(2026, 9, 5), overall_score=65.0)]
    )
    service.persist_snapshot(service.get_stock_research("AAPL"))

    history = service.get_research_history("AAPL")
    assert [entry.evaluation_date for entry in history] == [date(2026, 9, 5), date(2026, 8, 28)]


def test_get_research_history_is_empty_when_nothing_has_been_persisted(research_service):
    service, engine = research_service
    _seed_current_research(engine)
    assert service.get_research_history("AAPL") == []


def test_history_entries_distinguish_same_day_snapshots_by_created_at(research_service):
    """Regression test for the Snapshot History date bug: two snapshots
    saved on the same calendar day (evaluation_date only changes on a
    screener rebuild, so two same-day rebuilds with genuinely different
    content share it) must still be distinguishable in history by their
    actual persistence timestamp (`created_at`), not just `evaluation_date`
    -- the root cause was that the UI showed only evaluation_date, making
    distinct same-day snapshots look identical/stuck. Both fields must be
    present, correct, and `created_at` must actually differ and order
    newest-first alongside evaluation_date."""
    service, engine = research_service
    _seed_current_research(engine, evaluation_date=date(2026, 9, 5), overall_score=60.0)
    first = service.persist_snapshot(service.get_stock_research("AAPL"))

    Phase3Repository(engine).save_current_research(
        [_record("AAPL", evaluation_date=date(2026, 9, 5), overall_score=90.0)]
    )
    second = service.persist_snapshot(service.get_stock_research("AAPL"))

    # Genuinely different content -> genuinely different snapshots, even
    # though evaluation_date is identical for both.
    assert first.snapshot_id != second.snapshot_id
    assert first.evaluation_date == second.evaluation_date == date(2026, 9, 5)

    history = service.get_research_history("AAPL")
    assert len(history) == 2
    assert all(entry.created_at is not None for entry in history)
    # Newest-first ordering must be resolvable even with identical
    # evaluation_date, using created_at (and id) as the tiebreaker.
    assert history[0].snapshot_id == second.snapshot_id
    assert history[1].snapshot_id == first.snapshot_id
    assert history[0].created_at >= history[1].created_at


def test_persisting_identical_research_twice_does_not_duplicate_history(research_service):
    service, engine = research_service
    _seed_current_research(engine)
    research = service.get_stock_research("AAPL")

    service.persist_snapshot(research)
    service.persist_snapshot(research)

    assert len(service.get_research_history("AAPL")) == 1


def test_compare_snapshots_returns_none_when_either_id_is_missing(research_service):
    service, engine = research_service
    _seed_current_research(engine)
    summary = service.persist_snapshot(service.get_stock_research("AAPL"))

    assert service.compare_snapshots(summary.snapshot_id, "missing") is None
    assert service.compare_snapshots("missing", summary.snapshot_id) is None


def test_compare_snapshots_reports_the_score_change_between_two_persisted_states(
    research_service,
):
    service, engine = research_service
    _seed_current_research(engine, evaluation_date=date(2026, 8, 28), overall_score=60.0)
    older = service.persist_snapshot(service.get_stock_research("AAPL"))

    Phase3Repository(engine).save_current_research(
        [_record("AAPL", evaluation_date=date(2026, 9, 5), overall_score=72.0)]
    )
    newer = service.persist_snapshot(service.get_stock_research("AAPL"))

    comparison = service.compare_snapshots(older.snapshot_id, newer.snapshot_id)
    assert comparison is not None
    assert comparison.overall_score_old == pytest.approx(60.0)
    assert comparison.overall_score_new == pytest.approx(72.0)
    assert comparison.overall_score_changed is True


def test_get_stock_research_never_writes_a_historical_snapshot(research_service, monkeypatch):
    """Ordinary reads (what a Streamlit rerun does) must never persist
    history — only an explicit persist_snapshot call may write."""
    service, engine = research_service
    _seed_current_research(engine)

    def _fail(*args, **kwargs):
        raise AssertionError("get_stock_research must never write a snapshot")

    monkeypatch.setattr(service._snapshots, "save", _fail)

    service.get_stock_research("AAPL")
    service.list_current_research()
    service.get_research_history("AAPL")
    service.get_latest_snapshot("AAPL")
    # No assertion error raised above means no read path called save().


# --- historical snapshots must never pick up "current" supplemental state ---


def test_historical_snapshot_never_gains_supplemental_data_computed_after_it_was_saved(
    research_service,
):
    """A historical snapshot persisted before Analyst Consensus/Technical
    Summary/AI Research Rating existed for a ticker must still show them as
    None after those domains are later refreshed -- current-state tables
    must never leak into an already-persisted historical snapshot. Only
    the *current* view (get_stock_research) may reflect newly-refreshed
    supplemental data."""
    from datetime import timedelta

    import pandas as pd

    from alpha_lab.database.models import Price
    from alpha_lab.providers.base import MarketDataProvider
    from alpha_lab.research.supplemental_service import SupplementalResearchService

    service, engine = research_service
    _seed_current_research(engine)

    # Persist a historical snapshot while no supplemental data exists yet.
    pre_refresh_research = service.get_stock_research("AAPL")
    assert pre_refresh_research.analyst_consensus is None
    assert pre_refresh_research.technical_summary is None
    assert pre_refresh_research.ai_research_assessment is None
    saved = service.persist_snapshot(pre_refresh_research)

    # Now populate "current" supplemental state for the same ticker.
    with Session(engine) as session:
        for i in range(300):
            session.add(Price(
                ticker="AAPL", date=date(2025, 1, 1) + timedelta(days=i),
                close=100 + i * 0.5, high=101 + i * 0.5, low=99 + i * 0.5,
            ))
        session.commit()

    class _FakeProvider(MarketDataProvider):
        provider_name = "FakeProvider"

        def get_company_info(self, ticker):
            return {}

        def get_price_history(self, ticker, start, end):
            return pd.DataFrame()

        def get_financials(self, ticker):
            return pd.DataFrame()

        def get_analyst_consensus(self, ticker):
            return {
                "ticker": ticker, "as_of": date.today(), "strong_buy": 10, "buy": 5,
                "hold": 2, "sell": 0, "strong_sell": 0, "target_current": 100.0,
                "target_low": 90.0, "target_mean": 120.0, "target_median": 118.0,
                "target_high": 140.0, "source": "FakeProvider",
            }

    supplemental = SupplementalResearchService(engine)
    supplemental.refresh_analyst_consensus("AAPL", _FakeProvider())
    supplemental.refresh_technical_summary("AAPL")

    # The CURRENT view now reflects the freshly-refreshed data...
    post_refresh_research = service.get_stock_research("AAPL")
    assert post_refresh_research.analyst_consensus is not None
    assert post_refresh_research.technical_summary is not None

    # ...but the ALREADY-PERSISTED historical snapshot must not have
    # retroactively gained it.
    reloaded_snapshot = service.get_research_snapshot(saved.snapshot_id)
    assert reloaded_snapshot.analyst_consensus is None
    assert reloaded_snapshot.technical_summary is None
    assert reloaded_snapshot.ai_research_assessment is None

    latest = service.get_latest_snapshot("AAPL")
    assert latest.analyst_consensus is None
    assert latest.technical_summary is None
