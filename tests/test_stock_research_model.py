"""Unit tests for the canonical StockResearch conversion layer."""

from datetime import UTC, date, datetime, timedelta

import pytest

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


def test_missing_metric_evidence_timestamps_does_not_crash_and_yields_zero_freshness():
    from alpha_lab.research.build import _freshness_factor

    empty_categories = build_stock_research(_record()).categories
    assert _freshness_factor(_EVALUATION, empty_categories) == 0.0


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


# --- Regression coverage for the Codex review fixes on PR #13 -------------


def _record_with_single_metric(
    *, metric_name: str, category: str, value: float, period: date,
    provider: str = "SECCompanyFactsProvider", score: float | None = None,
    coverage: float = 0.0, percentile: float = 50.0, **overrides,
) -> LiveResearchRecord:
    category_scores = {name: None for name in CATEGORY_ORDER}
    category_coverage = {name: 0.0 for name in CATEGORY_ORDER}
    category_scores[category] = score
    category_coverage[category] = coverage
    return _record(
        category_scores=category_scores,
        category_coverage=category_coverage,
        raw_metrics={metric_name: value},
        percentile_metrics={metric_name: percentile},
        provenance={
            "metrics": {
                metric_name: {"provider": provider, "period": period.isoformat()},
            }
        },
        **overrides,
    )


# 1. Growth formula provenance must match alpha_lab.ratings.quality._growth
# exactly, including the sign-flip when the prior period is negative.


def test_eps_growth_formula_text_matches_calculate_quality_factors_for_all_sign_combinations():
    from alpha_lab.ratings.quality import calculate_quality_factors

    cases = [
        (120.0, 100.0, 0.2),  # positive -> positive
        (-120.0, -100.0, -0.2),  # negative -> negative
        (50.0, -100.0, 1.5),  # negative -> positive
        (-50.0, 100.0, -1.5),  # positive -> negative
    ]
    for current, prior, expected in cases:
        result = calculate_quality_factors({"eps": current}, {"eps": prior})
        assert result["eps_growth"] == pytest.approx(expected), (current, prior)
        # The documented formula (alpha_lab.research.formulas.FORMULAS["eps_growth"])
        # is "Current / abs(Prior) - (1 if Prior > 0 else -1)"; verify that
        # description independently reproduces the same engine output.
        documented = current / abs(prior) - (1 if prior > 0 else -1)
        assert documented == pytest.approx(expected)


def test_revenue_growth_formula_text_matches_calculate_quality_factors_for_all_sign_combinations():
    """Unlike eps_growth, revenue_growth's *current* side is filtered by
    alpha_lab.ratings.quality._positive() before the growth formula ever
    runs, so a non-positive current revenue is always None (not merely a
    sign-flip case) — the formula text documents this explicitly."""
    from alpha_lab.ratings.quality import calculate_quality_factors

    cases = [
        (1200.0, 1000.0, 0.2),  # positive current -> positive prior
        (500.0, -1000.0, 1.5),  # positive current -> negative prior
    ]
    for current, prior, expected in cases:
        result = calculate_quality_factors({"revenue": current}, {"revenue": prior})
        assert result["revenue_growth"] == pytest.approx(expected), (current, prior)

    for current in (-1200.0, 0.0):
        result = calculate_quality_factors({"revenue": current}, {"revenue": 1000.0})
        assert result["revenue_growth"] is None, current


def test_growth_is_undefined_not_zero_when_prior_is_zero_or_missing():
    from alpha_lab.ratings.quality import calculate_quality_factors

    assert calculate_quality_factors({"eps": 10.0}, {"eps": 0.0})["eps_growth"] is None
    assert calculate_quality_factors({"eps": 10.0}, {})["eps_growth"] is None
    assert calculate_quality_factors({"revenue": 10.0}, {"revenue": 0.0})["revenue_growth"] is None


# 2. debt_ebitda / cash_flow_to_debt must not claim net_debt as an input;
# the engine actually divides by gross total debt.


def test_debt_ebitda_and_cash_flow_to_debt_do_not_claim_net_debt_as_an_input():
    from alpha_lab.research.formulas import KNOWN_INPUT_METRICS

    assert "debt_ebitda" not in KNOWN_INPUT_METRICS
    assert "cash_flow_to_debt" not in KNOWN_INPUT_METRICS
    research = build_stock_research(
        _record_with_single_metric(
            metric_name="debt_ebitda", category="financial_strength",
            value=0.5, period=_EVALUATION,
        )
    )
    metric = next(
        m for m in research.categories["financial_strength"].metrics
        if m.name == "debt_ebitda"
    )
    assert metric.inputs is None


def test_debt_ebitda_and_cash_flow_to_debt_actually_use_gross_total_debt_not_net_debt():
    from alpha_lab.ratings.quality import calculate_quality_factors

    # cash is nonzero, so net_debt (= total_debt - cash) differs from total_debt.
    fundamentals = {
        "total_debt": 100.0, "cash": 40.0, "ebitda": 50.0, "free_cash_flow": 20.0,
    }
    result = calculate_quality_factors(fundamentals)
    assert result["net_debt"] == pytest.approx(60.0)
    assert result["debt_ebitda"] == pytest.approx(100.0 / 50.0)  # total_debt / ebitda
    assert result["cash_flow_to_debt"] == pytest.approx(20.0 / 100.0)  # fcf / total_debt
    assert result["debt_ebitda"] != pytest.approx(result["net_debt"] / 50.0)


# 3. Confidence freshness must come from evidence periods, never from
# Security.metadata_updated_at / LiveResearchRecord.last_refreshed.


def test_freshness_factor_is_full_for_genuinely_fresh_evidence():
    from alpha_lab.research.build import _freshness_factor

    categories = build_stock_research(
        _record_with_single_metric(
            metric_name="roe", category="business_quality",
            value=0.2, period=_EVALUATION - timedelta(days=5),
        )
    ).categories
    assert _freshness_factor(_EVALUATION, categories) == 1.0


def test_freshness_factor_decays_for_stale_evidence():
    from alpha_lab.research.build import _freshness_factor

    categories = build_stock_research(
        _record_with_single_metric(
            metric_name="roe", category="business_quality",
            value=0.2, period=_EVALUATION - timedelta(days=400),
        )
    ).categories
    assert _freshness_factor(_EVALUATION, categories) == 0.0


def test_recently_refreshed_metadata_cannot_rescue_confidence_for_year_old_financial_evidence():
    stale_evidence_fresh_metadata = _record_with_single_metric(
        metric_name="roe", category="business_quality", value=0.2,
        period=_EVALUATION - timedelta(days=400),
        overall_live_coverage=0.5,
        # The metadata/ingestion timestamp is recent, unlike the evidence itself.
        last_refreshed=datetime.combine(_EVALUATION, datetime.min.time(), tzinfo=UTC),
    )
    fresh_evidence_old_metadata = _record_with_single_metric(
        metric_name="roe", category="business_quality", value=0.2,
        period=_EVALUATION - timedelta(days=5),
        overall_live_coverage=0.5,
        last_refreshed=datetime(2020, 1, 1, tzinfo=UTC),
    )
    stale_research = build_stock_research(stale_evidence_fresh_metadata)
    fresh_research = build_stock_research(fresh_evidence_old_metadata)
    # A recent metadata refresh must not inflate confidence for stale evidence,
    # and a stale metadata timestamp must not depress confidence for genuinely
    # fresh evidence: only the evidence period drives the freshness factor.
    assert stale_research.confidence < fresh_research.confidence


# 4. Source-quality must reuse alpha_lab.providers.capabilities' field-level
# policy, not a second, coarser "trusted provider" list.


def test_reliable_provider_field_gets_full_capability_credit():
    from alpha_lab.research.build import _metric_capability_weight

    assert _metric_capability_weight("roe", "SECCompanyFactsProvider") == 1.0


def test_partial_provider_field_does_not_get_full_capability_credit():
    from alpha_lab.research.build import _metric_capability_weight

    assert _metric_capability_weight("roe", "YFinanceProvider") == 0.5


def test_price_evidence_credited_without_upgrading_unrelated_fundamentals_same_provider():
    from alpha_lab.research.build import _metric_capability_weight

    assert _metric_capability_weight("return_3m", "YFinanceProvider") == 1.0
    assert _metric_capability_weight("roe", "YFinanceProvider") == 0.5


def test_unknown_or_missing_provider_fails_closed_to_zero_capability_credit():
    from alpha_lab.research.build import _metric_capability_weight

    assert _metric_capability_weight("roe", "fixture") == 0.0
    assert _metric_capability_weight("roe", None) == 0.0


# 5. Category breadth for confidence must reflect evidence coverage, not
# merely whether a category cleared its minimum-metric threshold for a score.


def test_category_breadth_counts_partial_evidence_even_without_a_score():
    from alpha_lab.research.build import _confidence
    from alpha_lab.research.model import CategoryResult, CategoryStatus

    def categories_with(coverage: float, score: float | None) -> dict[str, CategoryResult]:
        status = CategoryStatus.UNAVAILABLE if score is None else CategoryStatus.PARTIAL
        template = CategoryResult(
            name="c", label="C", score=score, coverage=coverage, status=status,
            metrics=[], evidence=[], unavailable_metrics=[], sources=[],
        )
        return {name: template for name in CATEGORY_ORDER}

    base = _record(overall_live_coverage=0.5, data_quality_status="valid")
    zero_evidence = _confidence(base, categories_with(0.0, None))
    partial_no_score = _confidence(base, categories_with(0.3, None))
    partial_with_score = _confidence(base, categories_with(0.67, 75.0))
    full_evidence = _confidence(base, categories_with(1.0, 90.0))
    assert zero_evidence < partial_no_score < partial_with_score < full_evidence


# 6. INVALID contract: document (rather than silently rely on) the fact that
# the live rating engine currently coerces invalid inputs to None indistinct
# from genuinely missing ones, before anything reaches raw_metrics.


def test_invalid_inputs_are_coerced_to_none_indistinguishable_from_missing():
    """Pins the current contract so future INVALID wiring is a deliberate change.

    alpha_lab.ratings.quality/valuation already refuse to turn a non-finite or
    out-of-domain input into a plausible-looking ratio, but they do so by
    returning None — the same value used for "never reported". There is no
    signal in LiveResearchRecord.raw_metrics today that distinguishes the two,
    so alpha_lab.research.model.MetricStatus.INVALID stays unused/reserved
    (see the module docstring for the exact integration point).
    """
    from alpha_lab.ratings.quality import calculate_quality_factors

    result = calculate_quality_factors(
        {"revenue": float("nan"), "total_equity": -5.0, "net_income": 10.0}
    )
    assert result["gross_margin"] is None
    assert result["roe"] is None


# --- Regression coverage for the second Codex review round (commit 60a0b0d) --


def test_sec_sourced_valuation_ratios_get_full_capability_credit_not_zero():
    """pe/price_sales/ev_ebitda/price_fcf must credit whichever provider
    actually supplied their underlying fundamental field (per
    alpha_lab.screener.service._metric_provenance), not just YFinance."""
    from alpha_lab.research.build import _metric_capability_weight

    for metric in ("pe", "price_sales", "ev_ebitda", "price_fcf"):
        assert _metric_capability_weight(metric, "SECCompanyFactsProvider") == 1.0
        assert _metric_capability_weight(metric, "YFinanceProvider") == 0.5


def test_forward_pe_shares_the_estimates_capability_mapping():
    from alpha_lab.research.build import _metric_capability_weight

    assert _metric_capability_weight("forward_pe", "AlphaLabEstimateSnapshots") == 1.0
    assert _metric_capability_weight("forward_pe", "YFinanceProvider") == 0.5


def test_total_shareholder_yield_is_capped_regardless_of_recorded_provider():
    """Its provenance only ever records the dividends_paid provider even
    though share_repurchases may come from a different one, so it must not
    receive the same full credit dividend_yield/buyback_yield can earn."""
    from alpha_lab.research.build import _metric_capability_weight

    assert _metric_capability_weight("total_shareholder_yield", "SECCompanyFactsProvider") == 0.5
    assert _metric_capability_weight("total_shareholder_yield", "YFinanceProvider") == 0.5
    # Single-input siblings are unaffected and can still earn full credit.
    assert _metric_capability_weight("dividend_yield", "SECCompanyFactsProvider") == 1.0
    assert _metric_capability_weight("buyback_yield", "SECCompanyFactsProvider") == 1.0


def test_metric_evidence_retains_unrounded_percentile():
    research = build_stock_research(
        _record_with_single_metric(
            metric_name="roe", category="business_quality", value=0.34,
            period=_EVALUATION, score=None, coverage=0.0,
            percentile=91.456789,
        )
    )
    roe = next(
        m for m in research.categories["business_quality"].metrics if m.name == "roe"
    )
    assert roe.percentile == pytest.approx(91.456789)
    # The display bullet may round, but the structured field must not.
    assert any("91" in bullet for bullet in research.categories["business_quality"].evidence)


def test_unavailable_metric_percentile_is_none_not_a_stale_value():
    research = build_stock_research(_record())
    revisions = research.categories["analyst_revisions"]
    assert all(m.percentile is None for m in revisions.metrics)
