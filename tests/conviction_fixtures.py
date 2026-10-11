"""Shared hand-built fixtures for the Conviction/Conflict Layer tests
(offline: no network, no database). Imported by test_conviction.py and
test_conviction_audit.py."""

from datetime import UTC, date, datetime

from alpha_lab.alignment.alignment import Alignment, AlignmentAssessment, DonatienLean
from alpha_lab.config.settings import CoverageSettings
from alpha_lab.conviction import (
    FUNDAMENTAL_CATEGORIES,
    ConvictionAssessment,
    Family,
    FamilyAssessment,
    assess_conviction,
)
from alpha_lab.macro.regime import MacroRegime
from alpha_lab.research.ai_rating import (
    AIDimensionValue,
    AIEvidenceCoverage,
    AIResearchAssessment,
)
from alpha_lab.research.analyst_consensus import AnalystConsensus, AnalystRating
from alpha_lab.research.analyst_research import (
    AnalystResearchSummary,
    RevisionDirection,
    RevisionTrendPeriod,
)
from alpha_lab.research.model import (
    CategoryResult,
    CategoryStatus,
    ConfidenceBreakdown,
    StockResearch,
)
from alpha_lab.research.technical import TechnicalRating, TechnicalSummary, Timeframe
from alpha_lab.research_state import ResearchField, ResearchState

EVALUATION = date(2026, 9, 19)
COVERAGE = CoverageSettings()  # insufficient_below=0.40, full_confidence_at=0.70
STALE_DAYS = 5


# --- builders ---------------------------------------------------------------

def _category(name: str, score: float | None, *, coverage: float = 1.0, applicable: bool = True) -> CategoryResult:
    if not applicable:
        status = CategoryStatus.NOT_APPLICABLE
    elif score is None:
        status = CategoryStatus.UNAVAILABLE
    else:
        status = CategoryStatus.AVAILABLE if coverage >= 1.0 else CategoryStatus.PARTIAL
    return CategoryResult(
        name=name, label=name, score=score, coverage=coverage, status=status,
        metrics=[], evidence=[], unavailable_metrics=[], sources=[],
    )


def _categories(fundamental: float | list | None, *, coverage: float = 1.0, applicable: bool = True,
                extras: float = 50.0) -> dict[str, CategoryResult]:
    """`fundamental`: one score for all five fundamentals categories, or a list of five."""
    scores = fundamental if isinstance(fundamental, list) else [fundamental] * len(FUNDAMENTAL_CATEGORIES)
    categories = {
        name: _category(name, score, coverage=coverage, applicable=applicable)
        for name, score in zip(FUNDAMENTAL_CATEGORIES, scores)
    }
    for name in ("momentum", "analyst_revisions", "ai_research"):
        categories[name] = _category(name, extras)
    return categories


def _consensus(rating: AnalystRating, coverage: float = 1.0) -> AnalystConsensus:
    return AnalystConsensus(
        ticker="TST", rating=rating, total_analysts=10, as_of=EVALUATION,
        source="yfinance", coverage=coverage, confidence=1.0,
    )


def _revisions(direction: RevisionDirection) -> AnalystResearchSummary:
    period = RevisionTrendPeriod(
        fiscal_period=date(2026, 12, 31), eps_trend_current=1.0, eps_trend_7d_ago=1.0,
        eps_trend_30d_ago=0.9, eps_trend_60d_ago=None, eps_trend_90d_ago=None,
        revisions_up_last_7d=None, revisions_up_last_30d=None,
        revisions_down_last_7d=None, revisions_down_last_30d=None,
        direction=direction, observation_date=EVALUATION,
    )
    return AnalystResearchSummary(
        ticker="TST", recent_rating_changes=[], rating_change_counts_90d=None,
        revision_trend=[period], coverage=0.5, as_of=EVALUATION, evidence_ids=[],
    )


def _technical(rating: TechnicalRating, *, coverage: float = 1.0, as_of: date = EVALUATION,
               price_through: date | None = EVALUATION) -> TechnicalSummary:
    return TechnicalSummary(
        price_data_through=price_through,
        ticker="TST", overall_score=0.5, overall_rating=rating,
        moving_average_score=0.5, moving_average_rating=rating,
        oscillator_score=0.5, oscillator_rating=rating,
        indicators=[], moving_average_available=8, moving_average_total=8,
        oscillator_available=7, oscillator_total=7, coverage=coverage, confidence=1.0,
        timeframe=Timeframe.DAILY, as_of=as_of, source="yfinance",
    )


def _ai(rating: AIDimensionValue) -> AIResearchAssessment:
    return AIResearchAssessment(
        ticker="TST", score=None, rating=rating, confidence=0.8, dimensions={},
        evidence_coverage=AIEvidenceCoverage(
            fundamental_coverage=1.0, analyst_coverage=1.0, technical_coverage=1.0,
            overall_ai_evidence_coverage=1.0,
        ),
        positives=[], risks=[], catalysts=[], contradictions=[], evidence_gaps=[],
        supporting_evidence=[], prompt_version="v1", model="deterministic", model_fingerprint=None,
        research_schema_version="v1", generated_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC), as_of=EVALUATION,
        source="deterministic",
    )


def _research(
    *, fundamental: float | list | None = 80.0, fundamental_coverage: float = 1.0, applicable: bool = True,
    consensus: AnalystRating | None = AnalystRating.BUY, consensus_coverage: float = 1.0,
    revisions: RevisionDirection | None = RevisionDirection.IMPROVING,
    technical: TechnicalRating | None = TechnicalRating.BUY, technical_coverage: float = 1.0,
    price_through: date | None = EVALUATION, ai: AIDimensionValue | None = None,
    interpretation: str = "Strong", extras: float = 50.0,
) -> StockResearch:
    return StockResearch(
        ticker="TST", company_name="Test", sector="Technology", industry="Software", security_type="equity",
        categories=_categories(fundamental, coverage=fundamental_coverage, applicable=applicable, extras=extras),
        overall_score=70.0, overall_coverage=0.9, confidence=8.0, confidence_label="High confidence",
        score_interpretation=interpretation,
        confidence_breakdown=ConfidenceBreakdown(
            overall_coverage=0.9, category_breadth=0.9, freshness=1.0, source_quality=1.0,
            data_quality_penalty_applied=False,
        ),
        strengths=[], weaknesses=[], risks=[], catalysts=[], sources=["yfinance"],
        data_quality_status="valid", rating_version="v1", configuration_hash="abc",
        evaluation_date=EVALUATION, generated_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        analyst_consensus=None if consensus is None else _consensus(consensus, consensus_coverage),
        analyst_research=None if revisions is None else _revisions(revisions),
        technical_summary=None if technical is None else _technical(
            technical, coverage=technical_coverage, price_through=price_through
        ),
        ai_research_assessment=None if ai is None else _ai(ai),
    )


def _alignment(regime: MacroRegime | None, lean: DonatienLean | None = None) -> AlignmentAssessment:
    return AlignmentAssessment(
        as_of=EVALUATION, alignment=Alignment.NEUTRAL, market_regime=regime,
        market_regime_coverage=1.0 if regime else None, market_as_of=EVALUATION if regime else None,
        donatien_lean=lean, donatien_dominant_regime=None, donatien_confidence=None,
        donatien_defensiveness=None, donatien_scenario_weights=None,
        donatien_run_date=None, donatien_source_observed_at=None, donatien_retrieved_at=None,
    )


def _field(name: str, status: str = "FULL", value=None) -> ResearchField:
    return ResearchField(value=value, status=status, provenance_id=f"prov-{name}")


def _state(research: StockResearch | None, *, alignment: AlignmentAssessment | None = None,
           evaluation_date: date = EVALUATION) -> ResearchState:
    names = ("fundamentals", "analyst_activity", "technicals", "news", "news_impact", "macro", "donatien",
             "ethics", "deterministic_score", "ai_research", "ai_rating")
    fields = {name: _field(name) for name in names}
    fields["donatien_alignment"] = _field(
        "donatien_alignment", value=None if alignment is None else alignment.model_dump(mode="json")
    )
    return ResearchState(ticker="TST", evaluation_date=evaluation_date, stock_research=research, **fields)


def _assess(research: StockResearch | None, **kwargs) -> ConvictionAssessment:
    return assess_conviction(_state(research, **kwargs), coverage=COVERAGE, stale_price_days=STALE_DAYS)


def _family(assessment: ConvictionAssessment, family: Family) -> FamilyAssessment:
    return next(item for item in assessment.families if item.family is family)
