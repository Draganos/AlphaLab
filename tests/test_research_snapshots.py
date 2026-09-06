"""Focused tests for the append-only, immutable ResearchSnapshotRepository."""

from datetime import UTC, date, datetime

import pytest

from alpha_lab.database import create_schema, make_engine
from alpha_lab.research import CATEGORY_ORDER, RESEARCH_SCHEMA_VERSION, build_stock_research
from alpha_lab.research.snapshots import ResearchSnapshotRepository
from alpha_lab.screener import LiveResearchRecord

_EVAL_V1 = date(2026, 8, 28)
_EVAL_V2 = date(2026, 9, 5)


def _record(ticker: str, **overrides) -> LiveResearchRecord:
    defaults = dict(
        ticker=ticker,
        company=f"{ticker} Inc",
        price=100.0,
        market_cap=1_000_000.0,
        country="US",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        asset_type="equity",
        themes=[],
        ethical_status="PASS",
        data_quality_status="valid",
        overall_score=65.0,
        category_scores={name: None for name in CATEGORY_ORDER},
        category_coverage={name: 0.0 for name in CATEGORY_ORDER},
        raw_metrics={},
        percentile_metrics={},
        overall_live_coverage=0.5,
        quantitative_coverage=0.5,
        ai_coverage=0.0,
        historical_coverage=0.0,
        confidence="Positive",
        provenance={},
        last_refreshed=datetime(2026, 8, 20, tzinfo=UTC),
        rating_version="phase3-live-v2",
        configuration_hash="config-a",
        evaluation_date=_EVAL_V1,
    )
    defaults.update(overrides)
    return LiveResearchRecord.model_validate(defaults)


def _aapl_v1():
    scores = {name: None for name in CATEGORY_ORDER}
    coverage = {name: 0.0 for name in CATEGORY_ORDER}
    scores["business_quality"] = 70.0
    coverage["business_quality"] = 1.0
    record = _record(
        "AAPL",
        overall_score=65.0,
        overall_live_coverage=0.5,
        category_scores=scores,
        category_coverage=coverage,
        raw_metrics={"roe": 0.30},
        percentile_metrics={"roe": 80.0},
        provenance={"metrics": {"roe": {"provider": "SECCompanyFactsProvider", "period": "2025-06-30"}}},
        evaluation_date=_EVAL_V1,
    )
    return build_stock_research(record, generated_at=datetime(2026, 8, 28, 9, 0, tzinfo=UTC))


def _aapl_v2():
    """Deliberately differs from v1: changed score/coverage/confidence,
    a changed metric value, a newly available metric+category, a category
    status change (earnings_growth: UNAVAILABLE -> AVAILABLE), and one
    category left genuinely PARTIAL (valuation: some evidence, no score)."""
    scores = {name: None for name in CATEGORY_ORDER}
    coverage = {name: 0.0 for name in CATEGORY_ORDER}
    scores["business_quality"] = 75.0
    coverage["business_quality"] = 1.0
    scores["earnings_growth"] = 60.0
    coverage["earnings_growth"] = 1.0
    coverage["valuation"] = 0.4
    record = _record(
        "AAPL",
        overall_score=70.0,
        overall_live_coverage=0.6,
        category_scores=scores,
        category_coverage=coverage,
        raw_metrics={"roe": 0.32, "eps_growth": 0.10, "revenue_growth": 0.08, "pe": 28.0},
        percentile_metrics={"roe": 84.0, "eps_growth": 70.0, "revenue_growth": 65.0, "pe": 40.0},
        provenance={
            "metrics": {
                "roe": {"provider": "SECCompanyFactsProvider", "period": "2025-09-30"},
                "eps_growth": {"provider": "SECCompanyFactsProvider", "period": "2025-09-30"},
                "revenue_growth": {"provider": "SECCompanyFactsProvider", "period": "2025-09-30"},
                "pe": {"provider": "YFinanceProvider", "period": "2026-09-01"},
            }
        },
        evaluation_date=_EVAL_V2,
    )
    return build_stock_research(record, generated_at=datetime(2026, 9, 5, 9, 0, tzinfo=UTC))


def _nvda_v1():
    scores = {name: None for name in CATEGORY_ORDER}
    coverage = {name: 0.0 for name in CATEGORY_ORDER}
    scores["momentum"] = 82.0
    coverage["momentum"] = 1.0
    record = _record(
        "NVDA",
        overall_score=82.0,
        overall_live_coverage=0.4,
        category_scores=scores,
        category_coverage=coverage,
        raw_metrics={"return_3m": 0.25},
        percentile_metrics={"return_3m": 91.0},
        provenance={"metrics": {"return_3m": {"provider": "YFinanceProvider", "period": "2026-08-25"}}},
        evaluation_date=_EVAL_V1,
    )
    return build_stock_research(record, generated_at=datetime(2026, 8, 28, 9, 0, tzinfo=UTC))


@pytest.fixture
def repository(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'snapshots.db'}")
    create_schema(engine)
    try:
        yield ResearchSnapshotRepository(engine)
    finally:
        engine.dispose()


def test_save_then_get_round_trips_the_snapshot(repository):
    research = _aapl_v1()
    summary = repository.save(research)
    fetched = repository.get(summary.snapshot_id)
    assert fetched is not None
    assert fetched.ticker == "AAPL"
    assert fetched.overall_score == pytest.approx(65.0)


def test_get_unknown_snapshot_id_returns_none(repository):
    assert repository.get("does-not-exist") is None


def test_get_latest_returns_the_most_recent_by_evaluation_date(repository):
    repository.save(_aapl_v1())
    repository.save(_aapl_v2())
    latest = repository.get_latest("AAPL")
    assert latest.evaluation_date == _EVAL_V2
    assert latest.overall_score == pytest.approx(70.0)


def test_get_latest_for_unknown_ticker_returns_none(repository):
    assert repository.get_latest("MSFT") is None


def test_list_for_ticker_returns_newest_first_and_only_that_ticker(repository):
    repository.save(_aapl_v1())
    repository.save(_aapl_v2())
    repository.save(_nvda_v1())
    history = repository.list_for_ticker("AAPL")
    assert [entry.evaluation_date for entry in history] == [_EVAL_V2, _EVAL_V1]
    assert all(entry.ticker == "AAPL" for entry in history)


def test_list_for_ticker_respects_date_range_and_limit(repository):
    repository.save(_aapl_v1())
    repository.save(_aapl_v2())
    only_v1 = repository.list_for_ticker("AAPL", end=_EVAL_V1)
    assert [entry.evaluation_date for entry in only_v1] == [_EVAL_V1]
    limited = repository.list_for_ticker("AAPL", limit=1)
    assert len(limited) == 1
    assert limited[0].evaluation_date == _EVAL_V2


def test_deterministic_snapshot_identity_is_stable_for_identical_content(repository):
    first = repository.save(_aapl_v1())
    second = repository.save(_aapl_v1())
    assert first.snapshot_id == second.snapshot_id


def test_duplicate_identical_research_is_idempotent_not_duplicated(repository):
    repository.save(_aapl_v1())
    repository.save(_aapl_v1())
    repository.save(_aapl_v1())
    assert len(repository.list_for_ticker("AAPL")) == 1


def test_meaningfully_different_research_creates_a_new_snapshot(repository):
    repository.save(_aapl_v1())
    repository.save(_aapl_v2())
    history = repository.list_for_ticker("AAPL")
    assert len(history) == 2
    assert history[0].snapshot_id != history[1].snapshot_id


def test_identical_research_with_different_generated_at_is_still_idempotent(repository):
    """generated_at alone must never be the dedup key."""
    research = _aapl_v1()
    same_content_later = research.model_copy(
        update={"generated_at": research.generated_at.replace(hour=23)}
    )
    first = repository.save(research)
    second = repository.save(same_content_later)
    assert first.snapshot_id == second.snapshot_id
    assert len(repository.list_for_ticker("AAPL")) == 1


def test_persisted_snapshot_is_immutable_after_current_research_changes(repository):
    """Persisting v2 must not alter the already-persisted v1 row."""
    v1_summary = repository.save(_aapl_v1())
    repository.save(_aapl_v2())
    reloaded_v1 = repository.get(v1_summary.snapshot_id)
    assert reloaded_v1.overall_score == pytest.approx(65.0)
    assert reloaded_v1.evaluation_date == _EVAL_V1


def test_persistence_survives_service_recreation(tmp_path):
    """A fresh repository instance against the same database must see
    previously persisted snapshots — persistence is durable, not
    process-local state."""
    engine = make_engine(f"sqlite:///{tmp_path / 'durable.db'}")
    create_schema(engine)
    try:
        ResearchSnapshotRepository(engine).save(_aapl_v1())
        reopened = ResearchSnapshotRepository(engine)
        assert reopened.get_latest("AAPL") is not None
        assert len(reopened.list_for_ticker("AAPL")) == 1
    finally:
        engine.dispose()


def test_research_schema_version_is_recorded(repository):
    summary = repository.save(_aapl_v1())
    assert summary.research_schema_version == RESEARCH_SCHEMA_VERSION


def test_rating_version_and_configuration_hash_are_preserved(repository):
    research = _aapl_v1()
    summary = repository.save(research)
    assert summary.rating_version == research.rating_version
    assert summary.configuration_hash == research.configuration_hash
    reloaded = repository.get(summary.snapshot_id)
    assert reloaded.rating_version == research.rating_version
    assert reloaded.configuration_hash == research.configuration_hash


def test_payload_hash_differs_for_different_configuration_hash(repository):
    research = _aapl_v1()
    other_config = research.model_copy(update={"configuration_hash": "config-b"})
    first = repository.save(research)
    second = repository.save(other_config)
    assert first.snapshot_id != second.snapshot_id
    assert len(repository.list_for_ticker("AAPL")) == 2


def test_evidence_and_provenance_survive_serialization_round_trip(repository):
    research = _aapl_v2()
    summary = repository.save(research)
    reloaded = repository.get(summary.snapshot_id)
    original_metric = next(
        m for m in research.categories["business_quality"].metrics if m.name == "roe"
    )
    reloaded_metric = next(
        m for m in reloaded.categories["business_quality"].metrics if m.name == "roe"
    )
    assert reloaded_metric.value == original_metric.value
    assert reloaded_metric.unit == original_metric.unit
    assert reloaded_metric.percentile == original_metric.percentile
    assert reloaded_metric.status == original_metric.status
    assert reloaded_metric.source == original_metric.source
    assert reloaded_metric.period == original_metric.period
    assert reloaded_metric.formula == original_metric.formula
    assert reloaded_metric.inputs == original_metric.inputs
    assert reloaded_metric.is_calculated == original_metric.is_calculated


def test_missing_evidence_remains_missing_after_round_trip(repository):
    research = _aapl_v1()
    summary = repository.save(research)
    reloaded = repository.get(summary.snapshot_id)
    revisions = reloaded.categories["analyst_revisions"]
    assert revisions.score is None
    assert all(metric.value is None for metric in revisions.metrics)
    assert all(metric.status.value == "UNAVAILABLE" for metric in revisions.metrics)


def test_partial_status_is_preserved_after_round_trip(repository):
    research = _aapl_v2()
    summary = repository.save(research)
    reloaded = repository.get(summary.snapshot_id)
    # valuation has some evidence (pe) but not enough for a score -> PARTIAL.
    assert reloaded.categories["valuation"].status.value == "PARTIAL"
    assert reloaded.categories["valuation"].score is None
    assert reloaded.categories["business_quality"].status.value == "AVAILABLE"


def test_save_failure_propagates_rather_than_being_swallowed(repository, monkeypatch):
    from sqlalchemy.orm import Session

    def _boom(self, *args, **kwargs):
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(Session, "commit", _boom)
    with pytest.raises(RuntimeError, match="simulated storage failure"):
        repository.save(_aapl_v1())
    # And the failed write must not have silently landed anyway.
    assert repository.get_latest("AAPL") is None
