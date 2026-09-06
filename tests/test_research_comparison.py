"""Focused tests for the pure StockResearch comparison layer."""

from datetime import UTC, date, datetime

import pytest

from alpha_lab.research import CATEGORY_ORDER, build_stock_research
from alpha_lab.research.comparison import ChangeType, compare_stock_research
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


def _older():
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
        raw_metrics={"roe": 0.30, "operating_margin": 0.25},
        percentile_metrics={"roe": 80.0, "operating_margin": 60.0},
        provenance={
            "metrics": {
                "roe": {"provider": "SECCompanyFactsProvider", "period": "2025-06-30"},
                "operating_margin": {"provider": "SECCompanyFactsProvider", "period": "2025-06-30"},
            }
        },
        evaluation_date=_EVAL_V1,
    )
    return build_stock_research(record, generated_at=datetime(2026, 8, 28, 9, 0, tzinfo=UTC))


def _newer(**record_overrides):
    scores = {name: None for name in CATEGORY_ORDER}
    coverage = {name: 0.0 for name in CATEGORY_ORDER}
    scores["business_quality"] = 75.0
    coverage["business_quality"] = 1.0
    scores["earnings_growth"] = 60.0
    coverage["earnings_growth"] = 1.0
    coverage["valuation"] = 0.4
    defaults = dict(
        overall_score=70.0,
        overall_live_coverage=0.6,
        category_scores=scores,
        category_coverage=coverage,
        raw_metrics={"roe": 0.32, "eps_growth": 0.10, "pe": 28.0},
        percentile_metrics={"roe": 84.0, "eps_growth": 70.0, "pe": 40.0},
        provenance={
            "metrics": {
                "roe": {"provider": "SECCompanyFactsProvider", "period": "2025-09-30"},
                "eps_growth": {"provider": "SECCompanyFactsProvider", "period": "2025-09-30"},
                "pe": {"provider": "YFinanceProvider", "period": "2026-09-01"},
            }
        },
        evaluation_date=_EVAL_V2,
    )
    defaults.update(record_overrides)
    record = _record("AAPL", **defaults)
    return build_stock_research(record, generated_at=datetime(2026, 9, 5, 9, 0, tzinfo=UTC))


def test_compare_raises_for_different_tickers():
    older = _older()
    newer = _newer().model_copy(update={"ticker": "MSFT"})
    with pytest.raises(ValueError, match="different tickers"):
        compare_stock_research(older, newer)


def test_overall_score_coverage_and_confidence_changes_are_reported():
    comparison = compare_stock_research(_older(), _newer())
    assert comparison.overall_score_old == pytest.approx(65.0)
    assert comparison.overall_score_new == pytest.approx(70.0)
    assert comparison.overall_score_changed is True
    assert comparison.overall_coverage_changed is True
    assert comparison.confidence_changed is True


def test_no_change_when_comparing_identical_research_to_itself():
    research = _older()
    comparison = compare_stock_research(research, research)
    assert comparison.overall_score_changed is False
    assert comparison.overall_coverage_changed is False
    assert comparison.confidence_changed is False
    assert all(not category.metric_changes for category in comparison.categories)
    assert all(not category.score_changed for category in comparison.categories)


def test_rating_version_and_configuration_change_flags():
    older = _older()
    newer_config = _newer()
    same_config = compare_stock_research(older, newer_config)
    assert same_config.configuration_changed is False
    assert same_config.rating_version_changed is False

    different_config_record = _record(
        "AAPL", configuration_hash="config-b", evaluation_date=_EVAL_V2
    )
    different_config = build_stock_research(different_config_record)
    changed = compare_stock_research(older, different_config)
    assert changed.configuration_changed is True


def test_category_score_change_is_reported_without_coverage_change():
    comparison = compare_stock_research(_older(), _newer())
    business_quality = next(c for c in comparison.categories if c.category == "business_quality")
    assert business_quality.old_score == pytest.approx(70.0)
    assert business_quality.new_score == pytest.approx(75.0)
    assert business_quality.score_changed is True
    assert business_quality.coverage_changed is False
    assert business_quality.status_changed is False


def test_category_status_change_from_unavailable_to_available_is_reported():
    comparison = compare_stock_research(_older(), _newer())
    earnings_growth = next(c for c in comparison.categories if c.category == "earnings_growth")
    assert earnings_growth.old_status.value == "UNAVAILABLE"
    assert earnings_growth.new_status.value == "AVAILABLE"
    assert earnings_growth.status_changed is True
    assert earnings_growth.old_score is None
    assert earnings_growth.new_score == pytest.approx(60.0)


def test_category_status_change_from_unavailable_to_partial_is_not_treated_as_available():
    comparison = compare_stock_research(_older(), _newer())
    valuation = next(c for c in comparison.categories if c.category == "valuation")
    assert valuation.old_status.value == "UNAVAILABLE"
    assert valuation.new_status.value == "PARTIAL"
    assert valuation.status_changed is True
    assert valuation.new_score is None  # PARTIAL evidence completeness, not a score


def test_unchanged_category_has_no_metric_changes():
    comparison = compare_stock_research(_older(), _newer())
    momentum = next(c for c in comparison.categories if c.category == "momentum")
    assert momentum.status_changed is False
    assert momentum.score_changed is False
    assert momentum.metric_changes == []


def test_metric_value_changed_is_reported_with_old_and_new_values():
    comparison = compare_stock_research(_older(), _newer())
    business_quality = next(c for c in comparison.categories if c.category == "business_quality")
    roe_change = next(m for m in business_quality.metric_changes if m.metric == "roe")
    assert roe_change.change_type == ChangeType.VALUE_CHANGED
    assert roe_change.old_value == pytest.approx(0.30)
    assert roe_change.new_value == pytest.approx(0.32)


def test_metric_missing_to_value_is_not_reported_as_a_plain_increase():
    comparison = compare_stock_research(_older(), _newer())
    earnings_growth = next(c for c in comparison.categories if c.category == "earnings_growth")
    eps_growth_change = next(m for m in earnings_growth.metric_changes if m.metric == "eps_growth")
    assert eps_growth_change.change_type == ChangeType.MISSING_TO_VALUE
    assert eps_growth_change.old_value is None
    assert eps_growth_change.new_value == pytest.approx(0.10)


def test_metric_value_to_missing_is_reported_as_evidence_disappearing():
    comparison = compare_stock_research(_older(), _newer())
    business_quality = next(c for c in comparison.categories if c.category == "business_quality")
    operating_margin_change = next(
        m for m in business_quality.metric_changes if m.metric == "operating_margin"
    )
    assert operating_margin_change.change_type == ChangeType.VALUE_TO_MISSING
    assert operating_margin_change.old_value == pytest.approx(0.25)
    assert operating_margin_change.new_value is None
