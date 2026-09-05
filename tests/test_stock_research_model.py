"""Unit tests for the canonical StockResearch conversion layer."""

from datetime import UTC, date, datetime

from alpha_lab.research import (
    CATEGORY_ORDER,
    CategoryStatus,
    MetricStatus,
    build_stock_research,
)
from alpha_lab.screener import LiveResearchRecord

_EVALUATION = date(2026, 1, 15)


def _record(**overrides) -> LiveResearchRecord:
    defaults = dict(
        ticker="TEST",
        company="Test Co",
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
        overall_score=None,
        category_scores={name: None for name in CATEGORY_ORDER},
        category_coverage={name: 0.0 for name in CATEGORY_ORDER},
        raw_metrics={},
        percentile_metrics={},
        overall_live_coverage=0.0,
        quantitative_coverage=0.0,
        ai_coverage=0.0,
        historical_coverage=0.0,
        confidence="Insufficient data",
        provenance={},
        last_refreshed=datetime(2026, 1, 10, tzinfo=UTC),
        configuration_hash="fixture",
        evaluation_date=_EVALUATION,
    )
    defaults.update(overrides)
    return LiveResearchRecord(**defaults)


def _with_business_quality() -> LiveResearchRecord:
    category_scores = {name: None for name in CATEGORY_ORDER}
    category_coverage = {name: 0.0 for name in CATEGORY_ORDER}
    category_scores["business_quality"] = 85.0
    category_coverage["business_quality"] = 1.0
    return _record(
        overall_score=85.0,
        overall_live_coverage=0.4,
        category_scores=category_scores,
        category_coverage=category_coverage,
        raw_metrics={"roe": 0.34, "roa": 0.18},
        percentile_metrics={"roe": 91.0, "roa": 80.0},
        provenance={
            "metrics": {
                "roe": {
                    "provider": "SECCompanyFactsProvider",
                    "period": "2025-09-30",
                    "ingested_at": "2026-01-05T00:00:00",
                },
                "roa": {"provider": "SECCompanyFactsProvider"},
            }
        },
    )


def test_unavailable_category_keeps_score_none_not_zero_and_is_a_risk_not_a_weakness():
    record = _record()
    research = build_stock_research(record)
    revisions = research.categories["analyst_revisions"]
    assert revisions.status == CategoryStatus.UNAVAILABLE
    assert revisions.score is None
    assert all(m.status == MetricStatus.UNAVAILABLE for m in revisions.metrics)
    assert not any("Analyst Revisions" in w for w in research.weaknesses)
    assert any(
        "Analyst Revisions" in r and "excluded" in r for r in research.risks
    )
    assert not any("negative" in r and "excluded" not in r for r in research.risks)


def test_available_metric_carries_provenance_and_formula():
    research = build_stock_research(_with_business_quality())
    category = research.categories["business_quality"]
    assert category.status == CategoryStatus.AVAILABLE
    assert category.score == 85.0
    roe = next(m for m in category.metrics if m.name == "roe")
    assert roe.status == MetricStatus.AVAILABLE
    assert roe.value == 0.34
    assert roe.source == "SECCompanyFactsProvider"
    assert roe.formula == "Net Income / Total Equity"
    assert roe.is_calculated is True
    assert any("roe" in bullet for bullet in category.evidence)
    assert "SECCompanyFactsProvider" in category.sources


def test_strength_and_weakness_thresholds():
    strong = _with_business_quality()
    research = build_stock_research(strong)
    assert any("Business Quality" in s for s in research.strengths)
    assert not any("Business Quality" in w for w in research.weaknesses)

    category_scores = {name: None for name in CATEGORY_ORDER}
    category_scores["business_quality"] = 10.0
    category_coverage = {name: 0.0 for name in CATEGORY_ORDER}
    category_coverage["business_quality"] = 1.0
    weak = _record(category_scores=category_scores, category_coverage=category_coverage)
    weak_research = build_stock_research(weak)
    assert any("Business Quality" in w for w in weak_research.weaknesses)
    assert not any("Business Quality" in s for s in weak_research.strengths)

    category_scores["business_quality"] = 50.0
    neutral = _record(category_scores=category_scores, category_coverage=category_coverage)
    neutral_research = build_stock_research(neutral)
    assert not any("Business Quality" in s for s in neutral_research.strengths)
    assert not any("Business Quality" in w for w in neutral_research.weaknesses)


def test_confidence_is_separate_from_score_and_rises_with_coverage():
    low_coverage = _record(overall_score=90.0, overall_live_coverage=0.1)
    high_coverage = _record(overall_score=90.0, overall_live_coverage=0.9)
    low = build_stock_research(low_coverage)
    high = build_stock_research(high_coverage)
    assert low.overall_score == high.overall_score == 90.0
    assert low.confidence < high.confidence
    assert 0 <= low.confidence <= 10
    assert 0 <= high.confidence <= 10


def test_data_quality_issue_penalizes_confidence():
    valid = _record(overall_live_coverage=0.8, data_quality_status="valid")
    stale = _record(overall_live_coverage=0.8, data_quality_status="stale price")
    valid_research = build_stock_research(valid)
    stale_research = build_stock_research(stale)
    assert stale_research.confidence < valid_research.confidence
    assert any("stale price" in r for r in stale_research.risks)


def test_missing_refresh_timestamp_does_not_crash_and_lowers_confidence():
    fresh = _record(overall_live_coverage=0.5, last_refreshed=datetime(2026, 1, 14, tzinfo=UTC))
    missing = _record(overall_live_coverage=0.5, last_refreshed=None)
    fresh_research = build_stock_research(fresh)
    missing_research = build_stock_research(missing)
    assert missing_research.confidence < fresh_research.confidence


def test_categories_cover_all_eight_canonical_names_in_order():
    research = build_stock_research(_record())
    assert list(research.categories.keys()) == list(CATEGORY_ORDER)
    assert len(research.categories) == 8


def test_catalysts_are_never_fabricated_pending_ai_research():
    research = build_stock_research(_with_business_quality())
    assert research.catalysts == []


def test_generated_at_defaults_to_now_when_not_supplied():
    before = datetime.now(UTC)
    research = build_stock_research(_record())
    assert research.generated_at >= before
