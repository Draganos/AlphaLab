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
    name: str, label: str, *, coverage: float, metrics: list[MetricEvidence], sources: list[str],
    status: CategoryStatus | None = None,
) -> CategoryResult:
    status = status or (
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


def test_not_applicable_fundamental_category_is_never_shown_as_a_coverage_gap():
    """PR #29: a category alpha_lab.research.build classified NOT_APPLICABLE
    (e.g. valuation for an ETF) must read as NOT_APPLICABLE here too, never
    as NO_EVIDENCE -- the dashboard's whole point is to never present
    inapplicable evidence as though it were missing."""
    not_applicable_category = _category(
        "valuation", "Valuation", coverage=0.0,
        metrics=[_metric("pe", available=False)], sources=[],
        status=CategoryStatus.NOT_APPLICABLE,
    )
    research = _stock_research(categories={"valuation": not_applicable_category})
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "valuation")
    assert row.status == CoverageStatus.NOT_APPLICABLE
    assert row.limitation_reason == "not applicable to this security type"


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


def test_technical_row_still_explains_partial_coverage_above_the_gate():
    """Regression: a PARTIAL row that already clears MIN_COVERAGE_THRESHOLD
    must still carry a reason -- every other row builder in this module
    explains any non-FULL status, and this one silently left reason=None
    whenever coverage was PARTIAL-but-above-gate."""
    technical = TechnicalSummary(
        ticker="NVDA", overall_score=0.2, overall_rating=TechnicalRating.NEUTRAL,
        moving_average_score=0.2, moving_average_rating=TechnicalRating.NEUTRAL,
        oscillator_score=0.2, oscillator_rating=TechnicalRating.NEUTRAL,
        indicators=[], moving_average_available=5, moving_average_total=8,
        oscillator_available=4, oscillator_total=7, coverage=9 / 15, confidence=0.6,
        timeframe=Timeframe.DAILY, as_of=date(2026, 9, 17), source="yfinance",
    )
    research = _stock_research(technical_summary=technical)
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "technical")
    assert row.status == CoverageStatus.PARTIAL
    assert row.limitation_reason == "9/15 indicators available"


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


def test_ai_evidence_row_still_explains_partial_coverage_that_clears_both_gates():
    """Regression: a PARTIAL row that already clears both
    AI_MINIMUM_EVIDENCE_COVERAGE and AI_MINIMUM_ASSESSABLE_DIMENSIONS must
    still carry a reason -- this is exactly the real 5-ticker validation
    case (NVDA/MA/AAL at 87-92% coverage), where the row previously fell
    through both gate checks and silently left reason=None."""
    dimensions = {name: AIDimensionAssessment(value=AIDimensionValue.POSITIVE, confidence=0.5) for name in DIMENSION_NAMES}
    dimensions["risk_profile"] = AIDimensionAssessment(value=AIDimensionValue.REVIEW, confidence=0.0)
    assessment = AIResearchAssessment(
        ticker="NVDA", score=70.0, rating=AIDimensionValue.POSITIVE, confidence=0.8,
        dimensions=dimensions,
        evidence_coverage=AIEvidenceCoverage(
            fundamental_coverage=1.0, analyst_coverage=1.0, technical_coverage=0.75,
            overall_ai_evidence_coverage=0.92,
        ),
        positives=[], risks=[], catalysts=[], contradictions=[], evidence_gaps=[],
        supporting_evidence=["business_quality_score"], prompt_version="v1",
        model="deterministic", model_fingerprint=None, research_schema_version="v1",
        generated_at=datetime(2026, 9, 17, 12, 0), as_of=date(2026, 9, 17), source="deterministic",
    )
    research = _stock_research(ai_research_assessment=assessment)
    summary = build_security_coverage_summary(research)
    row = next(r for r in summary.rows if r.category == "ai_evidence")
    assert row.status == CoverageStatus.PARTIAL
    assert row.limitation_reason == "5/6 AI dimensions assessable"


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


def test_summarize_universe_breakdown_sorts_all_not_computed_groups_first():
    """A group with zero computed coverage (avg_coverage is NaN) must sort
    first, not last -- consistent with how NOT_COMPUTED already sorts
    ahead of PARTIAL/FULL on the Security Detail tab (_STATUS_ORDER): the
    weakest evidence state leads either view."""
    not_computed_summary = build_security_coverage_summary(
        _stock_research(ticker="GDX", categories={})
    )
    partial_category = _category(
        "business_quality", "Business Quality", coverage=0.5,
        metrics=[_metric("roe", available=True)], sources=["yfinance"],
    )
    partial_summary = build_security_coverage_summary(
        _stock_research(ticker="NVDA", categories={"business_quality": partial_category})
    )
    flat = flatten_coverage_rows([not_computed_summary, partial_summary])
    grouped = summarize_universe_breakdown(flat, "ticker")
    assert [row["ticker"] for row in grouped][0] == "GDX"


def test_summarize_universe_breakdown_excludes_not_applicable_rows_from_avg_coverage():
    """PR #29: a NOT_APPLICABLE row's coverage is a real 0.0, unlike
    NOT_COMPUTED's None/NaN -- it must still be excluded from avg_coverage,
    or an ETF-heavy group would read as artificially low-coverage on
    categories that were never applicable to it in the first place."""
    not_applicable_category = _category(
        "valuation", "Valuation", coverage=0.0, metrics=[], sources=[],
        status=CategoryStatus.NOT_APPLICABLE,
    )
    etf_summary = build_security_coverage_summary(
        _stock_research(ticker="GDX", categories={"valuation": not_applicable_category})
    )
    full_category = _category(
        "valuation", "Valuation", coverage=1.0,
        metrics=[_metric("pe", available=True)], sources=["yfinance"],
    )
    equity_summary = build_security_coverage_summary(
        _stock_research(ticker="NVDA", categories={"valuation": full_category})
    )
    flat = flatten_coverage_rows([etf_summary, equity_summary])
    by_category = {row["category"]: row for row in summarize_universe_breakdown(flat, "category")}
    row = by_category["valuation"]
    assert row["not_applicable"] == 1
    # Averaged over NVDA's 1.0 alone -- GDX's NOT_APPLICABLE 0.0 excluded,
    # not diluting the average to 0.5.
    assert row["avg_coverage"] == 1.0


def test_summarize_universe_breakdown_sorts_all_not_applicable_groups_last_not_first():
    """Regression: a group whose every row is NOT_APPLICABLE (e.g.
    'Valuation' grouped over a universe of nothing but ETFs) has
    avg_coverage == NaN for the same reason an all-NOT_COMPUTED group
    does, but must sort last (with FULL), never first as though it were
    the weakest group in the view."""
    not_applicable_category = _category(
        "valuation", "Valuation", coverage=0.0, metrics=[], sources=[],
        status=CategoryStatus.NOT_APPLICABLE,
    )
    # Both ETF summaries carry only a NOT_APPLICABLE "valuation" fundamental
    # category, plus the default analyst_consensus=None -> a genuinely
    # NOT_COMPUTED "analyst_consensus" row -- so the two category groups
    # this produces are each homogeneous, for a clean sort comparison.
    etf_summaries = [
        build_security_coverage_summary(
            _stock_research(ticker=ticker, categories={"valuation": not_applicable_category})
        )
        for ticker in ("FTEC", "GDX")
    ]
    flat = flatten_coverage_rows(etf_summaries)
    grouped_by_category = summarize_universe_breakdown(flat, "category")
    categories_in_order = [row["category"] for row in grouped_by_category]
    # Every other category here is genuinely NOT_COMPUTED (analyst_research
    # etc. were never supplied); valuation (entirely NOT_APPLICABLE) must
    # sort last, never ahead of any of them.
    assert categories_in_order[-1] == "valuation"
    assert categories_in_order[0] != "valuation"


def test_never_produces_a_second_overall_or_composite_score():
    """The dashboard is a read-model layer only -- SecurityCoverageSummary
    must never carry anything that looks like a new overall/composite
    score field."""
    research = _stock_research()
    summary = build_security_coverage_summary(research)
    assert not hasattr(summary, "overall_score")
    assert not hasattr(summary, "score")
    assert not hasattr(summary, "composite_score")
