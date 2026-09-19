"""Deterministic, offline tests for alpha_lab.research_stance's pure
build_research_stance function. No network, no database -- every object is
hand-built."""

from datetime import date, datetime

from alpha_lab.alignment.alignment import AlignmentAssessment, Alignment, DonatienLean
from alpha_lab.database.models import NewsArticleRecord
from alpha_lab.macro.regime import MacroRegime
from alpha_lab.research.ai_rating import AIDimensionValue, AIEvidenceCoverage, AIResearchAssessment
from alpha_lab.research.analyst_consensus import AnalystConsensus, AnalystRating
from alpha_lab.research.analyst_research import (
    AnalystResearchSummary,
    RevisionDirection,
    RevisionTrendPeriod,
)
from alpha_lab.research.model import ConfidenceBreakdown, StockResearch
from alpha_lab.research.technical import TechnicalRating, TechnicalSummary, Timeframe
from alpha_lab.research_stance import (
    ResearchStanceOutcome,
    StanceLean,
    build_research_stance,
)


def _stock_research(**overrides) -> StockResearch:
    base = dict(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        industry="Semiconductors",
        security_type="equity",
        categories={},
        overall_score=80.0,
        overall_coverage=0.9,
        confidence=8.0,
        confidence_label="High confidence",
        score_interpretation="Strong",
        confidence_breakdown=ConfidenceBreakdown(
            overall_coverage=0.9, category_breadth=0.9, freshness=1.0,
            source_quality=1.0, data_quality_penalty_applied=False,
        ),
        strengths=[], weaknesses=[], risks=[], catalysts=[],
        sources=["yfinance"], data_quality_status="valid",
        rating_version="v1", configuration_hash="abc",
        evaluation_date=date(2026, 9, 19), generated_at=datetime(2026, 9, 19, 12, 0),
    )
    base.update(overrides)
    return StockResearch(**base)


def _analyst_consensus(rating: AnalystRating, total_analysts: int | None = 10) -> AnalystConsensus:
    return AnalystConsensus(
        ticker="NVDA", rating=rating, total_analysts=total_analysts,
        as_of=date(2026, 9, 19), source="yfinance", coverage=1.0, confidence=1.0,
    )


def _revision_period(direction: RevisionDirection, fiscal_period: date) -> RevisionTrendPeriod:
    return RevisionTrendPeriod(
        fiscal_period=fiscal_period, eps_trend_current=1.0, eps_trend_7d_ago=1.0,
        eps_trend_30d_ago=0.9, eps_trend_60d_ago=None, eps_trend_90d_ago=None,
        revisions_up_last_7d=None, revisions_up_last_30d=None,
        revisions_down_last_7d=None, revisions_down_last_30d=None,
        direction=direction, observation_date=date(2026, 9, 19),
    )


def _analyst_research(*directions_by_period: tuple[RevisionDirection, date]) -> AnalystResearchSummary:
    trend = [_revision_period(direction, period) for direction, period in directions_by_period]
    return AnalystResearchSummary(
        ticker="NVDA", recent_rating_changes=[], rating_change_counts_90d=None,
        revision_trend=trend, coverage=0.5, as_of=date(2026, 9, 19), evidence_ids=[],
    )


def _technical_summary(rating: TechnicalRating) -> TechnicalSummary:
    return TechnicalSummary(
        ticker="NVDA", overall_score=0.5, overall_rating=rating,
        moving_average_score=0.5, moving_average_rating=rating,
        oscillator_score=0.5, oscillator_rating=rating,
        indicators=[], moving_average_available=8, moving_average_total=8,
        oscillator_available=7, oscillator_total=7,
        coverage=1.0, confidence=1.0, timeframe=Timeframe.DAILY,
        as_of=date(2026, 9, 19), source="yfinance",
    )


def _ai_assessment(rating: AIDimensionValue) -> AIResearchAssessment:
    return AIResearchAssessment(
        ticker="NVDA", score=None, rating=rating, confidence=0.8, dimensions={},
        evidence_coverage=AIEvidenceCoverage(
            fundamental_coverage=1.0, analyst_coverage=1.0, technical_coverage=1.0,
            overall_ai_evidence_coverage=1.0,
        ),
        positives=[], risks=[], catalysts=[], contradictions=[], evidence_gaps=[],
        supporting_evidence=[], prompt_version="v1", model="deterministic",
        model_fingerprint=None, research_schema_version="v1",
        generated_at=datetime(2026, 9, 19, 12, 0), as_of=date(2026, 9, 19), source="deterministic",
    )


def _alignment(
    *, market_regime: MacroRegime | None, donatien_lean: DonatienLean | None,
    alignment: Alignment = Alignment.NEUTRAL,
) -> AlignmentAssessment:
    return AlignmentAssessment(
        as_of=date(2026, 9, 19), alignment=alignment,
        market_regime=market_regime, market_regime_coverage=1.0 if market_regime else None,
        market_as_of=date(2026, 9, 19) if market_regime else None,
        donatien_lean=donatien_lean, donatien_dominant_regime=None, donatien_confidence=None,
        donatien_defensiveness=None, donatien_scenario_weights=None,
        donatien_run_date=None, donatien_source_observed_at=None, donatien_retrieved_at=None,
    )


def _news_article(published_at: datetime) -> NewsArticleRecord:
    return NewsArticleRecord(
        ticker="NVDA", content_hash=f"hash-{published_at.isoformat()}", title="Fixture article",
        publisher="Fixture Wire", url="https://example.com/a", summary=None,
        published_at=published_at, provider="fixture", raw_payload={},
    )


# --- fundamentals -----------------------------------------------------------

def test_fundamentals_line_reuses_score_interpretation_verbatim():
    research = _stock_research(score_interpretation="Strong")
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "fundamentals")
    assert line.label == "Strong"
    assert line.lean == StanceLean.POSITIVE


def test_fundamentals_provisional_prefix_is_stripped_for_lean_only():
    research = _stock_research(score_interpretation="Provisional — Weak")
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "fundamentals")
    assert line.label == "Provisional — Weak"  # display keeps the full string
    assert line.lean == StanceLean.NEGATIVE


def test_fundamentals_insufficient_data_never_treated_as_neutral():
    research = _stock_research(score_interpretation="Insufficient data")
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "fundamentals")
    assert line.lean == StanceLean.INSUFFICIENT_DATA


# --- analysts -----------------------------------------------------------

def test_analysts_line_is_not_computed_when_absent():
    stance = build_research_stance(_stock_research(analyst_consensus=None))
    line = next(line for line in stance.lines if line.domain == "analysts")
    assert line.label == "NOT_COMPUTED"
    assert line.lean == StanceLean.INSUFFICIENT_DATA


def test_analysts_line_handles_a_present_consensus_with_no_rating():
    """AnalystConsensus.rating is declared Optional on the model even
    though build_analyst_consensus itself never produces a bare None
    (REVIEW covers "couldn't be rated") -- a present-but-ratingless
    object must still degrade to NOT_COMPUTED, never crash the page."""
    ratingless = AnalystConsensus(ticker="NVDA", source="yfinance", coverage=0.5, confidence=0.5)
    stance = build_research_stance(_stock_research(analyst_consensus=ratingless))
    line = next(line for line in stance.lines if line.domain == "analysts")
    assert line.label == "NOT_COMPUTED"
    assert line.lean == StanceLean.INSUFFICIENT_DATA


def test_analysts_review_rating_is_insufficient_data_not_neutral():
    research = _stock_research(analyst_consensus=_analyst_consensus(AnalystRating.REVIEW))
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "analysts")
    assert line.label == "REVIEW"
    assert line.lean == StanceLean.INSUFFICIENT_DATA


def test_analysts_buy_rating_is_positive():
    research = _stock_research(analyst_consensus=_analyst_consensus(AnalystRating.BUY))
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "analysts")
    assert line.label == "BUY"
    assert line.lean == StanceLean.POSITIVE


# --- revisions -----------------------------------------------------------

def test_revisions_line_is_not_computed_when_no_trend_rows():
    research = _stock_research(analyst_research=_analyst_research())
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "revisions")
    assert line.label == "NOT_COMPUTED"


def test_revisions_uses_nearest_fiscal_period_not_the_furthest():
    research = _stock_research(analyst_research=_analyst_research(
        (RevisionDirection.IMPROVING, date(2026, 12, 31)),
        (RevisionDirection.DETERIORATING, date(2027, 12, 31)),
    ))
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "revisions")
    assert line.label == "IMPROVING"
    assert line.lean == StanceLean.POSITIVE


# --- technical -----------------------------------------------------------

def test_technical_review_is_insufficient_data():
    research = _stock_research(technical_summary=_technical_summary(TechnicalRating.REVIEW))
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "technical")
    assert line.lean == StanceLean.INSUFFICIENT_DATA


def test_technical_sell_is_negative():
    research = _stock_research(technical_summary=_technical_summary(TechnicalRating.SELL))
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "technical")
    assert line.label == "SELL"
    assert line.lean == StanceLean.NEGATIVE


# --- ai research -----------------------------------------------------------

def test_ai_research_very_positive_is_positive():
    research = _stock_research(ai_research_assessment=_ai_assessment(AIDimensionValue.VERY_POSITIVE))
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "ai_research")
    assert line.lean == StanceLean.POSITIVE


# --- macro / external calibration -----------------------------------------

def test_macro_and_calibration_are_not_computed_without_alignment():
    stance = build_research_stance(_stock_research())
    macro = next(line for line in stance.lines if line.domain == "macro")
    calibration = next(line for line in stance.lines if line.domain == "external_calibration")
    assert macro.label == "NOT_COMPUTED"
    assert calibration.label == "NOT_COMPUTED"


def test_macro_risk_off_is_negative_and_calibration_defensive_is_negative():
    alignment = _alignment(market_regime=MacroRegime.RISK_OFF, donatien_lean=DonatienLean.DEFENSIVE)
    stance = build_research_stance(_stock_research(), alignment=alignment)
    macro = next(line for line in stance.lines if line.domain == "macro")
    calibration = next(line for line in stance.lines if line.domain == "external_calibration")
    assert macro.label == "RISK_OFF" and macro.lean == StanceLean.NEGATIVE
    assert calibration.label == "DEFENSIVE" and calibration.lean == StanceLean.NEGATIVE


def test_macro_line_is_not_computed_when_alignment_has_no_market_regime():
    alignment = _alignment(market_regime=None, donatien_lean=DonatienLean.CONSTRUCTIVE)
    stance = build_research_stance(_stock_research(), alignment=alignment)
    macro = next(line for line in stance.lines if line.domain == "macro")
    calibration = next(line for line in stance.lines if line.domain == "external_calibration")
    assert macro.label == "NOT_COMPUTED"
    assert calibration.label == "CONSTRUCTIVE"  # independent of macro's own absence


# --- news (never directional) ----------------------------------------------

def test_news_is_never_directional_regardless_of_volume():
    stance_none = build_research_stance(_stock_research(), news_articles=None)
    stance_empty = build_research_stance(_stock_research(), news_articles=[])
    stance_present = build_research_stance(
        _stock_research(), news_articles=[_news_article(datetime(2026, 9, 1, 12, 0))]
    )
    for stance in (stance_none, stance_empty, stance_present):
        line = next(line for line in stance.lines if line.domain == "news")
        assert line.lean is None
    assert next(line for line in stance_none.lines if line.domain == "news").label == "NOT_COMPUTED"
    assert next(line for line in stance_empty.lines if line.domain == "news").label == "NO_EVIDENCE"
    present_line = next(line for line in stance_present.lines if line.domain == "news")
    assert present_line.label == "PRESENT"
    assert present_line.detail == "1 article(s)"


# --- coverage/confidence (never directional) --------------------------------

def test_coverage_confidence_is_never_directional():
    research = _stock_research(confidence_label="Low confidence")
    stance = build_research_stance(research)
    line = next(line for line in stance.lines if line.domain == "coverage_confidence")
    assert line.lean is None
    assert line.label == "Low confidence"


# --- outcome determination ---------------------------------------------------

def test_outcome_is_positive_when_no_directional_domain_is_negative():
    research = _stock_research(
        score_interpretation="Strong",
        analyst_consensus=_analyst_consensus(AnalystRating.BUY),
    )
    stance = build_research_stance(research)
    assert stance.outcome == ResearchStanceOutcome.POSITIVE
    assert stance.conflicts == []
    assert stance.primary_conflict is None


def test_outcome_is_negative_when_no_directional_domain_is_positive():
    research = _stock_research(
        score_interpretation="Weak",
        analyst_consensus=_analyst_consensus(AnalystRating.SELL),
    )
    stance = build_research_stance(research)
    assert stance.outcome == ResearchStanceOutcome.NEGATIVE


def test_outcome_is_mixed_positive_when_majority_positive_but_one_negative():
    research = _stock_research(
        score_interpretation="Strong",
        analyst_consensus=_analyst_consensus(AnalystRating.BUY),
        technical_summary=_technical_summary(TechnicalRating.SELL),
    )
    stance = build_research_stance(research)
    assert stance.outcome == ResearchStanceOutcome.MIXED_POSITIVE
    assert len(stance.conflicts) == 1
    assert stance.primary_conflict == stance.conflicts[0]
    assert "Technical" in stance.primary_conflict
    assert "positive" in stance.primary_conflict


def test_outcome_is_neutral_on_an_exact_tie_between_positive_and_negative():
    research = _stock_research(
        score_interpretation="Strong",
        technical_summary=_technical_summary(TechnicalRating.SELL),
    )
    stance = build_research_stance(research)
    assert stance.outcome == ResearchStanceOutcome.NEUTRAL
    assert len(stance.conflicts) == 1  # both directions still surfaced, never hidden


def test_outcome_is_insufficient_data_when_nothing_is_directional():
    research = _stock_research(score_interpretation="Insufficient data")
    stance = build_research_stance(research)
    assert stance.outcome == ResearchStanceOutcome.INSUFFICIENT_DATA
    assert stance.conflicts == []


def test_neutral_domains_never_trigger_a_conflict():
    research = _stock_research(
        score_interpretation="Neutral",
        analyst_consensus=_analyst_consensus(AnalystRating.NEUTRAL),
    )
    stance = build_research_stance(research)
    assert stance.outcome == ResearchStanceOutcome.INSUFFICIENT_DATA
    assert stance.conflicts == []


# --- never touches the fundamental score/categories -------------------------

def test_never_mutates_or_reads_back_into_overall_score_or_categories():
    research = _stock_research(overall_score=42.0, categories={})
    build_research_stance(research)
    assert research.overall_score == 42.0
    assert research.categories == {}
