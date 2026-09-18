"""Deterministic, offline tests for the Research Evidence & Coverage
Dashboard's pure read-model layer (alpha_lab.research.coverage_summary).
No network, no database -- every object is hand-built."""

from datetime import date, datetime
from types import SimpleNamespace

from alpha_lab.research.ai_rating import (
    AI_MINIMUM_EVIDENCE_COVERAGE,
    AIDimensionAssessment,
    AIDimensionValue,
    AIEvidenceCoverage,
    AIResearchAssessment,
)
from alpha_lab.research.analyst_consensus import AnalystConsensus
from alpha_lab.research.analyst_research import (
    AnalystResearchSummary,
    RatingChangeSummary,
    RevisionDirection,
    RevisionTrendPeriod,
)
from alpha_lab.evidence_coverage.summary import (
    CoverageStatus,
    build_security_coverage_summary,
    flatten_coverage_rows,
    summarize_universe_breakdown,
)
from alpha_lab.research.model import (
    CategoryResult,
    CategoryStatus,
    MetricEvidence,
    MetricStatus,
    StockResearch,
)
from alpha_lab.research.technical import TechnicalRating, TechnicalSummary, Timeframe

DIMENSION_NAMES = (
    "business_outlook",
    "growth_prospects",
    "competitive_position",
    "valuation_context",
    "risk_profile",
    "catalyst_strength",
)


def _metric(name: str, *, available: bool, retrieved_at: datetime | None = None) -> MetricEvidence:
    return MetricEvidence(
        name=name,
        value=1.0 if available else None,
        unit=None,
        source="yfinance" if available else None,
        retrieved_at=retrieved_at,
        is_calculated=False,
        status=MetricStatus.AVAILABLE if available else MetricStatus.UNAVAILABLE,
    )


def _category(
    name: str, label: str, *, coverage: float, metrics: list[MetricEvidence], sources: list[str]
) -> CategoryResult:
    status = (
        CategoryStatus.AVAILABLE
        if coverage >= 1.0
        else CategoryStatus.UNAVAILABLE
        if coverage <= 0.0
        else CategoryStatus.PARTIAL
    )
    return CategoryResult(
        name=name,
        label=label,
        score=50.0 if coverage > 0 else None,
        coverage=coverage,
        status=status,
        metrics=metrics,
        evidence=[],
        unavailable_metrics=[m.name for m in metrics if m.status != MetricStatus.AVAILABLE],
        sources=sources,
    )


def _stock_research(**overrides) -> StockResearch:
    full_metric = _metric("roe", available=True, retrieved_at=datetime(2026, 9, 1, 12, 0))
    empty_metric = _metric("debt_ratio", available=False)
    categories = {
        "business_quality": _category(
            "business_quality", "Business Quality", coverage=1.0,
            metrics=[full_metric], sources=["yfinance"],
        ),
        "valuation": _category(
            "valuation", "Valuation", coverage=0.0, metrics=[empty_metric], sources=[],
        ),
    }
    base = dict(
        ticker="NVDA",
        company_name="NVIDIA",
        sector="Technology",
        industry="Semiconductors",
        security_type="equity",
        categories=categories,
        overall_score=80.0,
        overall_coverage=0.5,
        confidence=7.0,
        confidence_label="Strong",
        score_interpretation="Strong",
        confidence_breakdown={
            "overall_coverage": 0.5,
            "category_breadth": 0.5,
            "freshness": 1.0,
            "source_quality": 1.0,
            "data_quality_penalty_applied": False,
        },
        strengths=[],
        weaknesses=[],
        risks=[],
        catalysts=[],
        sources=["yfinance"],
        data_quality_status="valid",
        rating_version="v1",
        configuration_hash="abc",
        evaluation_date=date(2026, 9, 17),
        generated_at=datetime(2026, 9, 17, 12, 0),
    )
    base.update(overrides)
    return StockResearch(**base)


def test_fundamental_category_rows_reflect_the_categorys_own_coverage():
    research = _stock_research()
    summary = build_security_coverage_summary(research)
    rows = {row.category: row for row in summary.rows}
    assert rows["business_quality"].coverage == 1.0
    assert rows["business_quality"].status == CoverageStatus.FULL
    assert rows["business_quality"].evidence_count == 1
    assert rows["business_quality"].freshness == datetime(2026, 9, 1, 12, 0)
    assert rows["valuation"].coverage == 0.0
    assert rows["valuation"].status == CoverageStatus.NO_EVIDENCE
    assert rows["valuation"].limitation_reason == "no metrics available for this category"


def test_analyst_consensus_row_is_not_computed_when_none():
    research = _stock_research(analyst_consensus=None)
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "analyst_consensus")
    assert row.status == CoverageStatus.NOT_COMPUTED
    assert row.coverage is None
    assert row.limitation_reason == "not computed for this research state"


def test_analyst_consensus_row_reads_genuine_coverage_and_count():
    consensus = AnalystConsensus(
        ticker="NVDA", rating=None, total_analysts=12, source="yfinance",
        as_of=date(2026, 9, 15), coverage=0.75, confidence=0.75,
    )
    research = _stock_research(analyst_consensus=consensus)
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "analyst_consensus")
    assert row.coverage == 0.75
    assert row.status == CoverageStatus.PARTIAL
    assert row.evidence_count == 12
    assert row.freshness == date(2026, 9, 15)
    assert row.providers == ["yfinance"]


def test_analyst_history_and_revisions_split_from_one_analyst_research_object():
    analyst_research = AnalystResearchSummary(
        ticker="NVDA",
        recent_rating_changes=[
            RatingChangeSummary(
                grade_date=date(2026, 9, 10), firm="Test Firm", to_grade="Buy",
                from_grade="Hold", action="up", price_target_action="Raises",
                current_price_target=100.0, prior_price_target=90.0,
            )
        ],
        rating_change_counts_90d={"upgrades": 1, "downgrades": 0, "initiations": 0, "reiterations": 0},
        revision_trend=[],
        coverage=0.5,
        as_of=date(2026, 9, 17),
        evidence_ids=["analyst_rating_change:1"],
    )
    research = _stock_research(analyst_research=analyst_research)
    summary = build_security_coverage_summary(research)
    history_row = next(r for r in summary.rows if r.category == "analyst_history")
    revisions_row = next(r for r in summary.rows if r.category == "revisions")
    assert history_row.coverage == 1.0
    assert history_row.evidence_count == 1
    assert history_row.freshness == date(2026, 9, 10)
    assert revisions_row.coverage == 0.0
    assert revisions_row.status == CoverageStatus.NO_EVIDENCE
    assert revisions_row.limitation_reason == "no EPS revision trend refreshed or confirmed no coverage"


def test_analyst_history_and_revisions_are_not_computed_when_analyst_research_is_none():
    research = _stock_research(analyst_research=None)
    summary = build_security_coverage_summary(research)
    for category in ("analyst_history", "revisions"):
        row = next(r for r in summary.rows if r.category == category)
        assert row.status == CoverageStatus.NOT_COMPUTED
        assert row.coverage is None


def test_technical_row_flags_below_minimum_coverage_gate():
    technical = TechnicalSummary(
        ticker="NVDA", overall_score=None, overall_rating=TechnicalRating.REVIEW,
        moving_average_score=None, moving_average_rating=TechnicalRating.REVIEW,
        oscillator_score=None, oscillator_rating=TechnicalRating.REVIEW,
        indicators=[], moving_average_available=1, moving_average_total=8,
        oscillator_available=1, oscillator_total=7, coverage=2 / 15, confidence=0.1,
        timeframe=Timeframe.DAILY, as_of=date(2026, 9, 17), source="yfinance",
    )
    research = _stock_research(technical_summary=technical)
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "technical")
    assert row.status == CoverageStatus.PARTIAL
    assert row.evidence_count == 2
    assert "minimum indicator coverage gate" in row.limitation_reason


def test_ai_evidence_row_reason_distinguishes_gate_from_dimension_shortfall():
    dimensions = {name: AIDimensionAssessment(value=AIDimensionValue.REVIEW, confidence=0.0) for name in DIMENSION_NAMES}
    dimensions["business_outlook"] = AIDimensionAssessment(value=AIDimensionValue.POSITIVE, confidence=0.5)
    assessment = AIResearchAssessment(
        ticker="NVDA", score=None, rating=AIDimensionValue.REVIEW, confidence=0.2,
        dimensions=dimensions,
        evidence_coverage=AIEvidenceCoverage(
            fundamental_coverage=0.5, analyst_coverage=0.5, technical_coverage=0.5,
            overall_ai_evidence_coverage=0.5,
        ),
        positives=[], risks=[], catalysts=[], contradictions=[], evidence_gaps=[],
        supporting_evidence=["business_quality_score"], prompt_version="v1",
        model="deterministic", model_fingerprint=None, research_schema_version="v1",
        generated_at=datetime(2026, 9, 17, 12, 0), as_of=date(2026, 9, 17), source="deterministic",
    )
    research = _stock_research(ai_research_assessment=assessment)
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "ai_evidence")
    assert row.coverage == 0.5
    assert row.evidence_count == 1
    assert "required" in row.limitation_reason and "dimensions assessable" in row.limitation_reason
    assert "gate" not in row.limitation_reason  # coverage 0.5 >= AI_MINIMUM_EVIDENCE_COVERAGE


def test_ai_evidence_row_reports_gate_reason_when_coverage_itself_is_too_low():
    dimensions = {name: AIDimensionAssessment(value=AIDimensionValue.REVIEW, confidence=0.0) for name in DIMENSION_NAMES}
    assessment = AIResearchAssessment(
        ticker="NVDA", score=None, rating=AIDimensionValue.REVIEW, confidence=0.0,
        dimensions=dimensions,
        evidence_coverage=AIEvidenceCoverage(
            fundamental_coverage=0.1, analyst_coverage=0.0, technical_coverage=0.0,
            overall_ai_evidence_coverage=AI_MINIMUM_EVIDENCE_COVERAGE / 2,
        ),
        positives=[], risks=[], catalysts=[], contradictions=[], evidence_gaps=[],
        supporting_evidence=[], prompt_version="v1", model="deterministic",
        model_fingerprint=None, research_schema_version="v1",
        generated_at=datetime(2026, 9, 17, 12, 0), as_of=date(2026, 9, 17), source="deterministic",
    )
    research = _stock_research(ai_research_assessment=assessment)
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "ai_evidence")
    assert "AI minimum evidence-coverage gate" in row.limitation_reason


def test_ai_evidence_row_never_carries_a_reason_when_status_is_full():
    """Regression: overall_ai_evidence_coverage (a separate average of raw
    domain coverage) can reach 1.0 -- CoverageStatus.FULL -- while only a
    minority of the 6 AI dimensions were assessable. A FULL row must never
    carry a gate/dimension-shortfall reason; the shortfall shows up
    elsewhere (AIResearchAssessment.rating itself), not as a contradiction
    on an otherwise "fully covered" coverage row."""
    dimensions = {name: AIDimensionAssessment(value=AIDimensionValue.REVIEW, confidence=0.0) for name in DIMENSION_NAMES}
    dimensions["business_outlook"] = AIDimensionAssessment(value=AIDimensionValue.POSITIVE, confidence=0.5)
    assessment = AIResearchAssessment(
        ticker="NVDA", score=None, rating=AIDimensionValue.REVIEW, confidence=0.2,
        dimensions=dimensions,
        evidence_coverage=AIEvidenceCoverage(
            fundamental_coverage=1.0, analyst_coverage=1.0, technical_coverage=1.0,
            overall_ai_evidence_coverage=1.0,
        ),
        positives=[], risks=[], catalysts=[], contradictions=[], evidence_gaps=[],
        supporting_evidence=["business_quality_score"], prompt_version="v1",
        model="deterministic", model_fingerprint=None, research_schema_version="v1",
        generated_at=datetime(2026, 9, 17, 12, 0), as_of=date(2026, 9, 17), source="deterministic",
    )
    research = _stock_research(ai_research_assessment=assessment)
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "ai_evidence")
    assert row.status == CoverageStatus.FULL
    assert row.limitation_reason is None


def test_news_row_distinguishes_not_queried_from_confirmed_zero():
    research = _stock_research()
    not_queried = build_security_coverage_summary(research, news_articles=None)
    confirmed_zero = build_security_coverage_summary(research, news_articles=[])
    row_not_queried = next(r for r in not_queried.rows if r.category == "news")
    row_zero = next(r for r in confirmed_zero.rows if r.category == "news")
    assert row_not_queried.status == CoverageStatus.NOT_COMPUTED
    assert row_zero.status == CoverageStatus.NO_EVIDENCE
    assert row_zero.coverage == 0.0


def test_news_row_reads_genuine_article_count_and_freshness():
    articles = [
        SimpleNamespace(provider="yfinance", published_at=datetime(2026, 9, 10, 8, 0)),
        SimpleNamespace(provider="yfinance", published_at=datetime(2026, 9, 15, 8, 0)),
    ]
    research = _stock_research()
    summary = build_security_coverage_summary(research, news_articles=articles)
    row = next(r for r in summary.rows if r.category == "news")
    assert row.coverage == 1.0
    assert row.evidence_count == 2
    assert row.freshness == datetime(2026, 9, 15, 8, 0)
    assert row.providers == ["yfinance"]


def test_macro_row_is_market_wide_and_never_fabricates_full_coverage():
    macro = SimpleNamespace(
        coverage=0.6, as_of=date(2026, 9, 17), source="alphalab-macro",
        indicators=[
            SimpleNamespace(signal=1),
            SimpleNamespace(signal=None),
            SimpleNamespace(signal=-1),
        ],
    )
    research = _stock_research()
    summary = build_security_coverage_summary(research, macro_assessment=macro)
    row = next(r for r in summary.rows if r.category == "macro")
    assert row.coverage == 0.6
    assert row.status == CoverageStatus.PARTIAL
    assert row.evidence_count == 2
    assert "market-wide" in row.limitation_reason


def test_macro_row_is_not_computed_when_omitted():
    research = _stock_research()
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "macro")
    assert row.status == CoverageStatus.NOT_COMPUTED


def test_flatten_coverage_rows_produces_one_dict_per_security_category_pair():
    research = _stock_research()
    summary = build_security_coverage_summary(research)
    flat = flatten_coverage_rows([summary])
    assert len(flat) == len(summary.rows)
    assert all(row["ticker"] == "NVDA" for row in flat)
    assert all(row["sector"] == "Technology" for row in flat)


def test_flatten_coverage_rows_explodes_multiple_providers_into_separate_rows():
    category = _category(
        "business_quality", "Business Quality", coverage=1.0,
        metrics=[_metric("roe", available=True)], sources=["yfinance", "sec_edgar"],
    )
    research = _stock_research(categories={"business_quality": category})
    summary = build_security_coverage_summary(research)
    flat = flatten_coverage_rows([summary])
    providers = {row["provider"] for row in flat if row["category"] == "business_quality"}
    assert providers == {"yfinance", "sec_edgar"}


def test_summarize_universe_breakdown_does_not_double_count_multi_provider_categories():
    """Regression: flatten_coverage_rows explodes one row per provider, so
    grouping directly on that exploded data by anything other than
    'provider' would double-count a (security, category) pair that cites
    more than one provider -- summarize_universe_breakdown must
    de-duplicate on (ticker, category) first for every non-provider
    breakdown."""
    multi_provider_category = _category(
        "business_quality", "Business Quality", coverage=1.0,
        metrics=[_metric("roe", available=True)], sources=["yfinance", "sec_edgar"],
    )
    single_provider_category = _category(
        "business_quality", "Business Quality", coverage=0.0, metrics=[], sources=["yfinance"],
    )
    nvda = build_security_coverage_summary(
        _stock_research(ticker="NVDA", categories={"business_quality": multi_provider_category})
    )
    aal = build_security_coverage_summary(
        _stock_research(ticker="AAL", categories={"business_quality": single_provider_category})
    )
    flat = flatten_coverage_rows([nvda, aal])

    by_category = {row["category"]: row for row in summarize_universe_breakdown(flat, "category")}
    row = by_category["business_quality"]
    assert row["rows"] == 2  # one per security, not one per (security, provider) pair
    assert row["avg_coverage"] == 0.5  # unweighted average of 1.0 (NVDA) and 0.0 (AAL)
    assert row["full_coverage"] == 1
    assert row["no_evidence"] == 1

    by_ticker = {row["ticker"]: row for row in summarize_universe_breakdown(flat, "ticker")}
    # Both securities carry the same number of category rows (one per
    # SecurityCoverageSummary row) regardless of NVDA's business_quality
    # category citing two providers -- never inflated by the explosion.
    assert by_ticker["NVDA"]["rows"] == len(nvda.rows) == by_ticker["AAL"]["rows"] == len(aal.rows)


def test_summarize_universe_breakdown_by_provider_counts_each_provider_once():
    """The one breakdown where the exploded (per-provider) rows are the
    correct input -- each contributing provider must be counted, not
    collapsed away by the (ticker, category) de-duplication used for every
    other breakdown."""
    category = _category(
        "business_quality", "Business Quality", coverage=1.0,
        metrics=[_metric("roe", available=True)], sources=["yfinance", "sec_edgar"],
    )
    research = _stock_research(categories={"business_quality": category})
    summary = build_security_coverage_summary(research)
    flat = flatten_coverage_rows([summary])

    by_provider = {row["provider"]: row for row in summarize_universe_breakdown(flat, "provider")}
    assert by_provider["yfinance"]["rows"] >= 1
    assert by_provider["sec_edgar"]["rows"] == 1


def test_summarize_universe_breakdown_returns_empty_list_for_no_rows():
    assert summarize_universe_breakdown([], "category") == []


def test_never_produces_a_second_overall_or_composite_score():
    """The dashboard is a read-model layer only -- SecurityCoverageSummary
    must never carry anything that looks like a new overall/composite
    score field."""
    research = _stock_research()
    summary = build_security_coverage_summary(research)
    assert not hasattr(summary, "overall_score")
    assert not hasattr(summary, "score")
    assert not hasattr(summary, "composite_score")
