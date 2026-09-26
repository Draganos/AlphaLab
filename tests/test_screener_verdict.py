"""Tests for alpha_lab.scorecard.verdict: SecurityScreenerVerdict's tier
classification, Fit Score blend, hard/caution gates, and AIFinalRating's
blend with a real AIResearchAnalysis. All inputs are hand-built fixtures --
no database, no provider call, no network."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from alpha_lab.database.models import AIResearchAnalysis
from alpha_lab.screener.service import LiveResearchRecord
from alpha_lab.scorecard.verdict import (
    FIT_SCORE_CATEGORIES,
    ScreenerVerdict,
    SecurityTier,
    build_ai_final_rating,
    build_security_screener_verdict,
    classify_tier,
)


_UNSET = object()


def _record(
    ticker: str = "TEST",
    *,
    market_cap: float | None = 50_000_000_000.0,
    # Defaults to market_cap itself -- i.e. "already USD", exactly what
    # every pre-FX-normalization fixture here implicitly assumed. Pass an
    # explicit value (e.g. None) only to simulate a currency known but not
    # yet convertible, or a real, already-converted USD-equivalent that
    # differs from the raw native-currency market_cap.
    market_cap_usd: float | None = _UNSET,
    currency: str | None = None,
    category_scores: dict[str, float | None] | None = None,
    raw_metrics: dict[str, float | int | None] | None = None,
    ethical_status: str = "PASS",
) -> LiveResearchRecord:
    scores = {name: 75.0 for name in FIT_SCORE_CATEGORIES}
    scores["ai_research"] = None
    if category_scores:
        scores.update(category_scores)
    if market_cap_usd is _UNSET:
        market_cap_usd = market_cap
    return LiveResearchRecord(
        ticker=ticker, company="Test Co", price=100.0, market_cap=market_cap,
        currency=currency, market_cap_usd=market_cap_usd,
        country="US", exchange="NASDAQ", sector="Technology", industry="Software",
        asset_type="EQUITY", themes=[], ethical_status=ethical_status,
        data_quality_status="valid", overall_score=70.0, overall_rank=None,
        category_scores=scores, category_coverage={},
        raw_metrics=raw_metrics or {}, percentile_metrics={},
        overall_live_coverage=1.0, quantitative_coverage=1.0, ai_coverage=0.0,
        historical_coverage=1.0, confidence="High", provenance={},
        last_refreshed=None, configuration_hash="abc123",
        evaluation_date=datetime.now(UTC).date(),
    )


def _ai_analysis(
    *,
    provider: str = "openai",
    ai_rating: float = 80.0,
    risk_score: float = 0.0,
    attributable: bool = True,
) -> AIResearchAnalysis:
    return AIResearchAnalysis(
        ticker="TEST",
        source_document_ids=[1, 2] if attributable else [],
        analyzed_document_ids=[1, 2] if attributable else None,
        input_fingerprint="fp" if attributable else None,
        component_scores={"risk_score": risk_score},
        key_positives=[], key_risks=[], evidence=[{"document_id": 1, "excerpt": "x"}],
        provider=provider, model="m", prompt_version="v1" if attributable else None,
        raw_output={}, ai_rating=ai_rating, confidence=0.8,
        analysis_date=datetime.now(UTC),
    )


# --- classify_tier -----------------------------------------------------------


def test_classify_tier_core_at_or_above_boundary():
    assert classify_tier(10_000_000_000.0) == SecurityTier.CORE


def test_classify_tier_growth_between_boundaries():
    assert classify_tier(5_000_000_000.0) == SecurityTier.GROWTH


def test_classify_tier_speculative_below_growth_boundary():
    assert classify_tier(1_000_000_000.0) == SecurityTier.SPECULATIVE


def test_classify_tier_unknown_market_cap_usd_defaults_to_speculative():
    # None covers both "market cap itself unknown" and "known but not yet
    # converted to USD" -- classify_tier cannot and does not distinguish
    # them; see its own docstring and alpha_lab.fx.FXRateService.
    assert classify_tier(None) == SecurityTier.SPECULATIVE


# --- build_security_screener_verdict: fit score / coverage -------------------


def test_full_coverage_high_scores_yields_strong_fit():
    verdict = build_security_screener_verdict(_record())
    assert verdict.fit_score is not None and verdict.fit_score >= 70
    assert verdict.verdict == ScreenerVerdict.STRONG_FIT
    assert verdict.dimension_coverage == 1.0


def test_low_scores_yield_no_interest():
    scores = {name: 10.0 for name in FIT_SCORE_CATEGORIES}
    verdict = build_security_screener_verdict(_record(category_scores=scores))
    assert verdict.fit_score is not None and verdict.fit_score < 40
    assert verdict.verdict == ScreenerVerdict.NO_INTEREST


def test_thin_coverage_below_minimum_yields_insufficient_data():
    scores = {name: None for name in FIT_SCORE_CATEGORIES}
    scores["earnings_growth"] = 90.0
    verdict = build_security_screener_verdict(_record(category_scores=scores))
    assert verdict.fit_score is None
    assert verdict.verdict == ScreenerVerdict.INSUFFICIENT_DATA
    assert verdict.dimension_scores == {"earnings_growth": 90.0}


def test_dimension_scores_omits_unavailable_categories_never_fabricates_zero():
    scores = {"valuation": None}
    verdict = build_security_screener_verdict(_record(category_scores=scores))
    assert "valuation" not in verdict.dimension_scores
    assert "earnings_growth" in verdict.dimension_scores


# --- tier-weighted blend differs by tier --------------------------------------


def test_tier_weighting_changes_fit_score_for_the_same_raw_categories():
    scores = {name: 50.0 for name in FIT_SCORE_CATEGORIES}
    scores["earnings_growth"] = 95.0  # SPECULATIVE weights this highest
    scores["financial_strength"] = 20.0  # CORE weights this highest (down here)
    core = build_security_screener_verdict(_record(market_cap=50_000_000_000.0, category_scores=scores))
    speculative = build_security_screener_verdict(_record(market_cap=500_000_000.0, category_scores=scores))
    assert core.tier == SecurityTier.CORE
    assert speculative.tier == SecurityTier.SPECULATIVE
    assert speculative.fit_score > core.fit_score


def test_non_usd_market_cap_without_a_real_fx_rate_is_never_classified_core():
    # A real AED-denominated large-cap (e.g. a UAE security) whose currency
    # is known but has no FX rate ingested yet (alpha_lab.fx.FXRateService
    # then leaves market_cap_usd as None -- see MarketScreenerService's own
    # wiring) must not be classified CORE just because its raw, native
    # market_cap number happens to clear the USD CORE threshold.
    verdict = build_security_screener_verdict(
        _record(market_cap=50_000_000_000.0, currency="AED", market_cap_usd=None)
    )
    assert verdict.tier == SecurityTier.SPECULATIVE


def test_non_usd_market_cap_is_classified_correctly_once_really_fx_converted():
    # The positive case full FX normalization exists for: once a real FX
    # rate IS ingested for a non-USD currency, MarketScreenerService
    # converts the raw native market_cap to a real market_cap_usd, and
    # classify_tier -- which only ever sees that already-converted number --
    # classifies it exactly like any other USD market cap of the same size.
    verdict = build_security_screener_verdict(
        _record(
            market_cap=183_625_000_000.0,  # ~50B USD at a real AED peg (~0.2724)
            currency="AED",
            market_cap_usd=50_000_000_000.0,
        )
    )
    assert verdict.tier == SecurityTier.CORE


# --- hard gates ---------------------------------------------------------------


def test_ethical_exclusion_is_a_hard_gate_regardless_of_fit_score():
    verdict = build_security_screener_verdict(_record(ethical_status="EXCLUDED"))
    assert "ethical_exclusion" in verdict.hard_gates
    assert verdict.verdict == ScreenerVerdict.NO_INTEREST


def test_ethical_review_is_not_treated_as_an_exclusion():
    """REVIEW means "not yet determined", not "determined excluded" --
    a real EthicalStatus distinct from EXCLUDED (alpha_lab.ethics.policy).
    It must never force NO_INTEREST the way a real exclusion does."""
    verdict = build_security_screener_verdict(_record(ethical_status="REVIEW"))
    assert verdict.hard_gates == []
    assert "ethical_exclusion" not in verdict.hard_gates
    assert verdict.verdict != ScreenerVerdict.NO_INTEREST


def test_ethical_review_pending_caps_verdict_at_interest_not_strong_fit():
    verdict = build_security_screener_verdict(_record(ethical_status="REVIEW"))
    assert "ethical_review_pending" in verdict.caution_gates
    assert verdict.fit_score >= 70  # would otherwise be STRONG_FIT
    assert verdict.verdict == ScreenerVerdict.INTEREST


def test_ethical_unknown_also_caps_verdict_like_review():
    verdict = build_security_screener_verdict(_record(ethical_status="UNKNOWN"))
    assert "ethical_review_pending" in verdict.caution_gates
    assert verdict.verdict == ScreenerVerdict.INTEREST


def test_severe_leverage_is_a_hard_gate():
    verdict = build_security_screener_verdict(_record(raw_metrics={"debt_ebitda": 8.0}))
    assert "severe_leverage" in verdict.hard_gates
    assert verdict.verdict == ScreenerVerdict.NO_INTEREST


def test_interest_coverage_shortfall_is_a_hard_gate():
    verdict = build_security_screener_verdict(_record(raw_metrics={"interest_coverage": 0.5}))
    assert "interest_coverage_shortfall" in verdict.hard_gates
    assert verdict.verdict == ScreenerVerdict.NO_INTEREST


def test_healthy_leverage_metrics_trigger_no_hard_gate():
    verdict = build_security_screener_verdict(
        _record(raw_metrics={"debt_ebitda": 2.0, "interest_coverage": 10.0})
    )
    assert verdict.hard_gates == []


# --- caution gates -------------------------------------------------------------


def test_extreme_valuation_caps_verdict_at_interest_despite_high_fit_score():
    scores = {name: 90.0 for name in FIT_SCORE_CATEGORIES}
    scores["valuation"] = 5.0
    verdict = build_security_screener_verdict(_record(category_scores=scores))
    assert "valuation_extreme" in verdict.caution_gates
    assert verdict.fit_score >= 70  # would otherwise be STRONG_FIT
    assert verdict.verdict == ScreenerVerdict.INTEREST


def test_negative_revisions_caution_gate():
    scores = {name: 90.0 for name in FIT_SCORE_CATEGORIES}
    scores["analyst_revisions"] = 5.0
    verdict = build_security_screener_verdict(_record(category_scores=scores))
    assert "negative_revisions" in verdict.caution_gates


def test_weak_balance_sheet_relative_caution_gate():
    scores = {name: 90.0 for name in FIT_SCORE_CATEGORIES}
    scores["financial_strength"] = 10.0
    verdict = build_security_screener_verdict(_record(category_scores=scores))
    assert "weak_balance_sheet_relative" in verdict.caution_gates


# --- build_ai_final_rating -----------------------------------------------------


def test_final_rating_falls_back_to_fit_score_when_ai_is_none():
    verdict = build_security_screener_verdict(_record())
    rating = build_ai_final_rating(verdict, None)
    assert rating.ai_rating is None
    assert rating.final_rating == verdict.fit_score


def test_final_rating_falls_back_to_fit_score_when_ai_not_scoring_eligible():
    """RuleBasedFinancialResearchProvider-style analyses stay excluded from
    scoring here too -- same trust boundary as `overall_score`."""
    verdict = build_security_screener_verdict(_record())
    ai = _ai_analysis(provider="RuleBasedFinancialResearchProvider")
    rating = build_ai_final_rating(verdict, ai)
    assert rating.ai_rating is None
    assert rating.final_rating == verdict.fit_score


def test_final_rating_falls_back_to_fit_score_when_ai_not_attributable():
    verdict = build_security_screener_verdict(_record())
    ai = _ai_analysis(attributable=False)
    rating = build_ai_final_rating(verdict, ai)
    assert rating.ai_rating is None
    assert rating.final_rating == verdict.fit_score


def test_final_rating_blends_fit_score_and_ai_rating_when_both_usable():
    verdict = build_security_screener_verdict(_record())  # fit_score ~75
    ai = _ai_analysis(provider="openai", ai_rating=50.0)
    rating = build_ai_final_rating(verdict, ai)
    assert rating.ai_rating == 50.0
    assert rating.ai_provider == "openai"
    # blend is strictly between the two inputs, weighted toward fit_score
    assert min(verdict.fit_score, 50.0) < rating.final_rating < max(verdict.fit_score, 50.0)
    assert abs(rating.final_rating - (verdict.fit_score * 0.7 + 50.0 * 0.3)) < 0.01


def test_ai_never_rescues_an_insufficient_quantitative_scorecard():
    """A real, scoring-eligible AI rating must never manufacture a
    complete scorecard when the quantitative Fit Score itself is
    INSUFFICIENT_DATA (fewer than _MIN_FIT_SCORE_CATEGORIES available) --
    AI is combined with the quantitative Fit Score, never a substitute
    for it."""
    scores = {name: None for name in FIT_SCORE_CATEGORIES}
    scores["earnings_growth"] = 90.0
    verdict = build_security_screener_verdict(_record(category_scores=scores))
    assert verdict.fit_score is None  # confirms the premise
    ai = _ai_analysis(provider="openai", ai_rating=95.0)
    rating = build_ai_final_rating(verdict, ai)
    assert rating.ai_rating == 95.0  # still reported for transparency
    assert rating.final_rating is None
    assert rating.final_verdict == ScreenerVerdict.INSUFFICIENT_DATA


def test_hard_gate_forces_no_interest_even_with_a_strong_ai_rating():
    verdict = build_security_screener_verdict(_record(raw_metrics={"debt_ebitda": 9.0}))
    ai = _ai_analysis(provider="openai", ai_rating=99.0)
    rating = build_ai_final_rating(verdict, ai)
    assert rating.final_verdict == ScreenerVerdict.NO_INTEREST
    assert "severe_leverage" in rating.gates_triggered


def test_elevated_ai_risk_score_caps_final_verdict_at_interest():
    verdict = build_security_screener_verdict(_record())  # STRONG_FIT-eligible fit_score
    ai = _ai_analysis(provider="openai", ai_rating=90.0, risk_score=1.8)
    rating = build_ai_final_rating(verdict, ai)
    assert "elevated_ai_risk_signal" in rating.gates_triggered
    assert rating.final_verdict == ScreenerVerdict.INTEREST


def test_low_ai_risk_score_does_not_trigger_the_risk_gate():
    verdict = build_security_screener_verdict(_record())
    ai = _ai_analysis(provider="openai", ai_rating=90.0, risk_score=0.2)
    rating = build_ai_final_rating(verdict, ai)
    assert "elevated_ai_risk_signal" not in rating.gates_triggered
    assert rating.final_verdict == ScreenerVerdict.STRONG_FIT


# --- import boundary, mirroring test_macro_regression.py's own guard ---------


def test_no_scoring_module_imports_the_scorecard_verdict_code():
    """alpha_lab.scorecard reads FROM alpha_lab.screener (LiveResearchRecord,
    the AI eligibility gates); nothing in the reverse direction may ever
    import it back -- LiveResearchRecord.overall_score must stay untouched
    by this module."""
    root = Path(__file__).resolve().parents[1]
    scoring_dirs = ["alpha_lab/research", "alpha_lab/screener", "alpha_lab/strategy",
                     "alpha_lab/backtest", "alpha_lab/portfolio", "alpha_lab/ratings",
                     "alpha_lab/factors"]
    offenders = []
    for directory in scoring_dirs:
        for path in (root / directory).rglob("*.py"):
            if "alpha_lab.scorecard" in path.read_text():
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"Scoring modules must never import the scorecard verdict code: {offenders}"
