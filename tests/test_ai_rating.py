"""Deterministic, offline tests for AI Research Rating: structured output
validation, the evidence boundary (fabricated citations rejected), honest
multi-domain evidence usage, the minimum-evidence gate on the overall
rating, domain-aware evidence coverage, deterministic score/confidence
mapping, REVIEW behaviour, and provenance. No network call is ever made --
DeterministicAIRatingProvider is rule-based, and OpenAIRatingProvider is
never invoked in these tests."""

from datetime import date

import pytest
from pydantic import ValidationError

from alpha_lab.research.ai_rating import (
    AI_MINIMUM_ASSESSABLE_DIMENSIONS,
    AI_MINIMUM_EVIDENCE_COVERAGE,
    AI_RATING_METHODOLOGY_VERSION,
    AIDimensionAssessment,
    AIDimensionValue,
    AIEvidenceCoverage,
    AIEvidenceItem,
    AIRawDimensions,
    DeterministicAIRatingProvider,
    EvidenceViolation,
    build_ai_research_assessment,
    build_evidence_coverage,
    build_evidence_payload,
    compute_confidence,
    compute_score,
    validate_evidence_ids,
)

DIMENSION_NAMES = (
    "business_outlook",
    "growth_prospects",
    "competitive_position",
    "valuation_context",
    "risk_profile",
    "catalyst_strength",
)


def _dimension(value=AIDimensionValue.NEUTRAL, confidence=0.5, evidence_ids=None) -> AIDimensionAssessment:
    return AIDimensionAssessment(
        value=value, confidence=confidence, supporting_evidence_ids=evidence_ids or []
    )


def _raw(**overrides) -> AIRawDimensions:
    base = {name: _dimension() for name in DIMENSION_NAMES}
    base["provider"] = "test-provider"
    base["model"] = "test-model"
    base.update(overrides)
    return AIRawDimensions(**base)


def _coverage(fundamental=1.0, analyst=1.0, technical=1.0) -> AIEvidenceCoverage:
    return AIEvidenceCoverage(
        fundamental_coverage=fundamental,
        analyst_coverage=analyst,
        technical_coverage=technical,
        overall_ai_evidence_coverage=(fundamental + analyst + technical) / 3,
    )


def _all_dimensions(value: AIDimensionValue, **overrides) -> AIRawDimensions:
    merged = {name: _dimension(value) for name in DIMENSION_NAMES}
    merged.update(overrides)
    return _raw(**merged)


# --- structured output validation -----------------------------------------


def test_raw_dimensions_rejects_unknown_fields():
    """extra='forbid' -- a provider cannot smuggle an unstructured field
    (e.g. a free-text 'price_target') past the schema."""
    with pytest.raises(ValidationError):
        AIRawDimensions(
            **{name: _dimension() for name in DIMENSION_NAMES},
            provider="x",
            model="y",
            price_target=123.45,
        )


def test_dimension_confidence_must_be_within_zero_one():
    with pytest.raises(ValidationError):
        AIDimensionAssessment(value=AIDimensionValue.POSITIVE, confidence=1.5)


def test_review_is_a_valid_dimension_value():
    dimension = _dimension(value=AIDimensionValue.REVIEW, confidence=0.0)
    assert dimension.value == AIDimensionValue.REVIEW


# --- evidence boundary: fabricated citations are rejected, never silently corrected ---


def test_evidence_ids_within_the_supplied_payload_are_accepted():
    evidence = [AIEvidenceItem(evidence_id="fundamental:business_quality", description="x", source="y")]
    raw = _raw(business_outlook=_dimension(evidence_ids=["fundamental:business_quality"]))
    validate_evidence_ids(raw, {item.evidence_id for item in evidence})  # must not raise


def test_evidence_id_outside_the_payload_is_rejected_not_dropped():
    evidence = [AIEvidenceItem(evidence_id="fundamental:business_quality", description="x", source="y")]
    raw = _raw(business_outlook=_dimension(evidence_ids=["fabricated:price_target"]))
    with pytest.raises(EvidenceViolation):
        validate_evidence_ids(raw, {item.evidence_id for item in evidence})


def test_build_assessment_raises_on_fabricated_evidence_rather_than_correcting_it():
    evidence = [AIEvidenceItem(evidence_id="fundamental:business_quality", description="x", source="y")]
    raw = _raw(growth_prospects=_dimension(evidence_ids=["metric:made_up_metric"]))
    with pytest.raises(EvidenceViolation):
        build_ai_research_assessment(
            ticker="NVDA", raw=raw, evidence=evidence, evidence_coverage=_coverage(),
            research_schema_version="stockresearch-v2", as_of=date.today(),
        )


# --- deterministic score mapping: the provider never invents a number ------


def test_compute_score_is_the_documented_scaling_of_the_dimension_mean():
    dimensions = {
        "a": _dimension(AIDimensionValue.VERY_POSITIVE),
        "b": _dimension(AIDimensionValue.VERY_POSITIVE),
    }
    # mean = 2.0 -> (2+2)/4*100 = 100
    assert compute_score(dimensions) == pytest.approx(100.0)


def test_compute_score_ignores_review_dimensions_in_the_mean():
    dimensions = {
        "a": _dimension(AIDimensionValue.POSITIVE),
        "b": _dimension(AIDimensionValue.REVIEW),
    }
    # only "a" (value=1) counts: (1+2)/4*100 = 75
    assert compute_score(dimensions) == pytest.approx(75.0)


def test_compute_score_is_none_when_every_dimension_is_review():
    dimensions = {name: _dimension(AIDimensionValue.REVIEW) for name in ("a", "b")}
    assert compute_score(dimensions) is None


def test_build_assessment_never_invents_a_score_when_all_dimensions_are_review():
    raw = _all_dimensions(AIDimensionValue.REVIEW)
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=_coverage(0.0, 0.0, 0.0),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.score is None
    assert assessment.rating == AIDimensionValue.REVIEW


@pytest.mark.parametrize(
    "value,expected_rating",
    [
        (AIDimensionValue.VERY_POSITIVE, AIDimensionValue.VERY_POSITIVE),
        (AIDimensionValue.POSITIVE, AIDimensionValue.POSITIVE),
        (AIDimensionValue.NEUTRAL, AIDimensionValue.NEUTRAL),
        (AIDimensionValue.NEGATIVE, AIDimensionValue.NEGATIVE),
        (AIDimensionValue.VERY_NEGATIVE, AIDimensionValue.VERY_NEGATIVE),
    ],
)
def test_uniform_dimension_values_map_to_the_matching_rating_band_given_sufficient_evidence(value, expected_rating):
    """All six dimensions assessable and full coverage -- comfortably above
    the minimum-evidence gate, so the score/rating mapping is exercised
    directly."""
    raw = _all_dimensions(value)
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=_coverage(1.0, 1.0, 1.0),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == expected_rating
    assert assessment.score is not None


# --- minimum evidence gate: sparse evidence must never produce a confident rating ---


def test_one_positive_dimension_with_five_review_forces_overall_review_not_positive():
    """The exact scenario from the correction request: business_outlook =
    VERY_POSITIVE alone, everything else REVIEW, must NOT become a
    substantive positive overall rating."""
    raw = _all_dimensions(AIDimensionValue.REVIEW, business_outlook=_dimension(AIDimensionValue.VERY_POSITIVE, confidence=0.9))
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=_coverage(1 / 8, 0.0, 0.0),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == AIDimensionValue.REVIEW
    assert assessment.score is None
    # Confidence may still be low-but-nonzero -- that combination is fine.
    assert assessment.confidence >= 0.0


def test_review_with_low_confidence_is_a_valid_combination():
    raw = _all_dimensions(AIDimensionValue.REVIEW, business_outlook=_dimension(AIDimensionValue.VERY_POSITIVE, confidence=0.9))
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=_coverage(1 / 8, 0.0, 0.0),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == AIDimensionValue.REVIEW
    assert 0.0 < assessment.confidence < 0.5


def test_below_minimum_assessable_dimensions_forces_review_even_with_full_coverage():
    """Only two of six dimensions assessable -- below
    AI_MINIMUM_ASSESSABLE_DIMENSIONS -- must be REVIEW even though the
    caller-supplied coverage is high."""
    assert AI_MINIMUM_ASSESSABLE_DIMENSIONS >= 3
    raw = _all_dimensions(AIDimensionValue.REVIEW, business_outlook=_dimension(AIDimensionValue.POSITIVE), growth_prospects=_dimension(AIDimensionValue.POSITIVE))
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=_coverage(1.0, 1.0, 1.0),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == AIDimensionValue.REVIEW
    assert assessment.score is None


def test_below_minimum_evidence_coverage_forces_review_even_with_enough_dimensions():
    """All six dimensions assessable but overall_ai_evidence_coverage is
    below AI_MINIMUM_EVIDENCE_COVERAGE -- still REVIEW. Guards against a
    provider that fabricates six confident dimensions from threadbare
    evidence."""
    assert AI_MINIMUM_EVIDENCE_COVERAGE > 0
    raw = _all_dimensions(AIDimensionValue.POSITIVE)
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[],
        evidence_coverage=_coverage(0.05, 0.0, 0.0),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == AIDimensionValue.REVIEW
    assert assessment.score is None


def test_meeting_both_minimums_produces_a_normal_rating():
    raw = _all_dimensions(AIDimensionValue.POSITIVE)
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=_coverage(1.0, 1.0, 1.0),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == AIDimensionValue.POSITIVE
    assert assessment.score is not None


# --- confidence: separate from rating, never freely asserted by the provider ---


def test_confidence_is_zero_when_all_dimensions_are_review_regardless_of_self_reported_confidence():
    dimensions = {
        name: _dimension(AIDimensionValue.REVIEW, confidence=0.99)
        for name in ("a", "b")
    }
    confidence = compute_confidence(dimensions, evidence_count=0, evidence_coverage=0.0)
    assert confidence == 0.0


def test_confidence_increases_with_evidence_coverage_and_density():
    dimensions = {"a": _dimension(AIDimensionValue.POSITIVE, confidence=0.8)}
    low = compute_confidence(dimensions, evidence_count=1, evidence_coverage=0.1)
    high = compute_confidence(dimensions, evidence_count=6, evidence_coverage=1.0)
    assert high > low


# --- provenance --------------------------------------------------------


def test_provenance_fields_are_recorded():
    raw = _raw(provider="deterministic-rule-based", model="category-threshold-v2")
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=_coverage(),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.source == "deterministic-rule-based"
    assert assessment.model == "category-threshold-v2"
    assert assessment.methodology_version == AI_RATING_METHODOLOGY_VERSION
    assert assessment.research_schema_version == "stockresearch-v2"
    assert assessment.generated_at is not None


def test_evidence_coverage_is_recorded_on_the_assessment():
    raw = _all_dimensions(AIDimensionValue.POSITIVE)
    coverage = _coverage(1.0, 0.5, 0.0)
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=coverage,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.evidence_coverage == coverage


def test_supporting_evidence_is_deduplicated_and_sorted():
    evidence = [
        AIEvidenceItem(evidence_id="fundamental:business_quality", description="x", source="y"),
        AIEvidenceItem(evidence_id="fundamental:valuation", description="x", source="y"),
    ]
    raw = _raw(
        business_outlook=_dimension(evidence_ids=["fundamental:business_quality"]),
        valuation_context=_dimension(evidence_ids=["fundamental:business_quality", "fundamental:valuation"]),
    )
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=evidence, evidence_coverage=_coverage(),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.supporting_evidence == ["fundamental:business_quality", "fundamental:valuation"]


def test_fundamental_score_is_never_touched_by_building_an_assessment():
    """A pure function -- must not mutate any input, including a
    dict-of-CategoryResult-like structure if one were passed via evidence."""
    raw = _all_dimensions(AIDimensionValue.POSITIVE)
    evidence_before = [AIEvidenceItem(evidence_id="fundamental:business_quality", description="x", source="y", value=80.0)]
    build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=evidence_before, evidence_coverage=_coverage(),
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert evidence_before[0].value == 80.0
    assert evidence_before[0].evidence_id == "fundamental:business_quality"


# --- domain-aware evidence coverage: missing domains count as 0, never excluded ---


def test_missing_analyst_and_technical_coverage_is_zero_not_excluded():
    coverage = build_evidence_coverage(fundamental_coverage=1.0, analyst_consensus=None, technical_summary=None)
    assert coverage.fundamental_coverage == 1.0
    assert coverage.analyst_coverage == 0.0
    assert coverage.technical_coverage == 0.0
    # Averaged over exactly three domains, not one -- 100% fundamental alone
    # must not read as 100% overall AI evidence coverage.
    assert coverage.overall_ai_evidence_coverage == pytest.approx(1 / 3)


class _FakeAnalystConsensus:
    def __init__(self, coverage):
        self.coverage = coverage


class _FakeTechnicalSummary:
    def __init__(self, coverage):
        self.coverage = coverage


def test_present_analyst_and_technical_coverage_is_used_directly():
    coverage = build_evidence_coverage(
        fundamental_coverage=1.0,
        analyst_consensus=_FakeAnalystConsensus(0.8),
        technical_summary=_FakeTechnicalSummary(0.6),
    )
    assert coverage.analyst_coverage == 0.8
    assert coverage.technical_coverage == 0.6
    assert coverage.overall_ai_evidence_coverage == pytest.approx((1.0 + 0.8 + 0.6) / 3)


def test_full_fundamental_coverage_alone_does_not_read_as_full_ai_evidence_coverage():
    """The exact scenario from the correction request."""
    coverage = build_evidence_coverage(fundamental_coverage=1.0, analyst_consensus=None, technical_summary=None)
    assert coverage.overall_ai_evidence_coverage < 1.0


# --- evidence payload construction: the only thing a provider ever sees ---


class _FakeStatus:
    def __init__(self, value):
        self.value = value


class _FakeMetric:
    def __init__(self, name, value, unit=None, source=None):
        self.name, self.value, self.unit, self.source = name, value, unit, source


class _FakeCategory:
    def __init__(self, label, score, coverage, status, metrics=None):
        self.label, self.score, self.coverage = label, score, coverage
        self.status = _FakeStatus(status)
        self.metrics = metrics or []


class _FakeRatingValue:
    def __init__(self, value):
        self.value = value


class _FakeConsensus:
    def __init__(self, rating="BUY", total_analysts=20, upside_to_mean=0.15, coverage=1.0):
        self.rating = _FakeRatingValue(rating)
        self.total_analysts = total_analysts
        self.upside_to_mean = upside_to_mean
        self.coverage = coverage


class _FakeTechnical:
    def __init__(self, overall="BUY", ma="BUY", osc="NEUTRAL", coverage=1.0):
        self.overall_rating = _FakeRatingValue(overall)
        self.moving_average_rating = _FakeRatingValue(ma)
        self.oscillator_rating = _FakeRatingValue(osc)
        self.coverage = coverage


def test_evidence_payload_excludes_unavailable_categories_entirely():
    categories = {
        "business_quality": _FakeCategory("Business Quality", 75.0, 1.0, "AVAILABLE"),
        "valuation": _FakeCategory("Valuation", None, 0.0, "UNAVAILABLE"),
    }
    items = build_evidence_payload(categories=categories)
    ids = {item.evidence_id for item in items}
    assert "fundamental:business_quality" in ids
    assert "fundamental:valuation" not in ids


def test_evidence_payload_includes_available_metrics_but_not_missing_ones():
    categories = {
        "business_quality": _FakeCategory(
            "Business Quality", 75.0, 0.5, "PARTIAL",
            metrics=[_FakeMetric("roe", 0.32, unit="ratio"), _FakeMetric("roic", None)],
        ),
    }
    items = build_evidence_payload(categories=categories)
    ids = {item.evidence_id for item in items}
    assert "metric:business_quality:roe" in ids
    assert "metric:business_quality:roic" not in ids


def test_evidence_payload_never_includes_pretrained_knowledge_only_structured_facts():
    """Every item must trace back to a real AlphaLab source string -- there
    is no code path here that injects free-text company narrative."""
    categories = {"business_quality": _FakeCategory("Business Quality", 75.0, 1.0, "AVAILABLE")}
    items = build_evidence_payload(categories=categories)
    assert all(item.source for item in items)


def test_evidence_payload_carries_analyst_and_technical_evidence_when_supplied():
    items = build_evidence_payload(
        categories={}, analyst_consensus=_FakeConsensus(), technical_summary=_FakeTechnical()
    )
    ids = {item.evidence_id for item in items}
    assert "analyst:rating" in ids
    assert "analyst:upside_to_mean" in ids
    assert "technical:overall_rating" in ids


def test_evidence_payload_omits_analyst_and_technical_evidence_when_not_supplied():
    items = build_evidence_payload(categories={})
    ids = {item.evidence_id for item in items}
    assert not any(i.startswith("analyst:") or i.startswith("technical:") for i in ids)


# --- DeterministicAIRatingProvider: honest, multi-domain, offline default ---


def test_deterministic_provider_derives_positive_dimension_from_high_category_score():
    categories = {"business_quality": _FakeCategory("Business Quality", 80.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(categories=categories)
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    assert raw.business_outlook.value == AIDimensionValue.POSITIVE
    assert raw.business_outlook.supporting_evidence_ids == ["fundamental:business_quality"]


def test_deterministic_provider_derives_very_negative_dimension_from_very_low_category_score():
    categories = {"earnings_growth": _FakeCategory("Earnings Growth", 5.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(categories=categories)
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    assert raw.growth_prospects.value == AIDimensionValue.VERY_NEGATIVE


def test_deterministic_provider_reviews_dimensions_with_no_evidence_and_reports_the_gap():
    raw = DeterministicAIRatingProvider().assess("NVDA", [])
    assert raw.business_outlook.value == AIDimensionValue.REVIEW
    assert raw.business_outlook.confidence == 0.0
    assert len(raw.evidence_gaps) == 6


def test_deterministic_provider_never_cites_evidence_it_was_not_given():
    categories = {"business_quality": _FakeCategory("Business Quality", 80.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(
        categories=categories, analyst_consensus=_FakeConsensus(), technical_summary=_FakeTechnical()
    )
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    allowed = {item.evidence_id for item in evidence}
    validate_evidence_ids(raw, allowed)  # must not raise


def test_deterministic_provider_uses_analyst_consensus_for_business_outlook_when_supplied():
    """business_outlook may blend fundamental business_quality with
    Analyst Consensus -- but only when Analyst Consensus was actually
    supplied."""
    categories = {"business_quality": _FakeCategory("Business Quality", 80.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(categories=categories, analyst_consensus=_FakeConsensus(rating="STRONG_BUY"))
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    assert "analyst:rating" in raw.business_outlook.supporting_evidence_ids
    assert "fundamental:business_quality" in raw.business_outlook.supporting_evidence_ids


def test_deterministic_provider_never_cites_analyst_consensus_when_not_supplied():
    """Must not imply it analysed Analyst Consensus when no such evidence
    was ever given -- the exact honesty requirement from the correction
    request."""
    categories = {"business_quality": _FakeCategory("Business Quality", 80.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(categories=categories)  # no analyst_consensus argument
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    assert "analyst:rating" not in raw.business_outlook.supporting_evidence_ids
    assert not any(item.evidence_id.startswith("analyst:") for item in evidence)


def test_deterministic_provider_never_uses_technical_summary_for_a_fundamental_business_dimension():
    """Technical Summary must feed only catalyst_strength, never
    competitive_position or business_outlook -- a moving-average signal is
    not a fact about the business."""
    categories = {"business_quality": _FakeCategory("Business Quality", 80.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(
        categories=categories, technical_summary=_FakeTechnical(overall="STRONG_SELL")
    )
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    # business_quality alone is POSITIVE; a contradicting STRONG_SELL
    # technical signal must not have leaked into this dimension.
    assert raw.business_outlook.value == AIDimensionValue.POSITIVE
    assert not any(eid.startswith("technical:") for eid in raw.business_outlook.supporting_evidence_ids)
    assert not any(eid.startswith("technical:") for eid in raw.competitive_position.supporting_evidence_ids)


def test_deterministic_provider_uses_technical_summary_for_catalyst_strength():
    evidence = build_evidence_payload(categories={}, technical_summary=_FakeTechnical(overall="STRONG_BUY"))
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    assert raw.catalyst_strength.value == AIDimensionValue.VERY_POSITIVE
    assert raw.catalyst_strength.supporting_evidence_ids == ["technical:overall_rating"]


def test_deterministic_provider_records_a_contradiction_when_sources_disagree():
    categories = {"business_quality": _FakeCategory("Business Quality", 90.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(categories=categories, analyst_consensus=_FakeConsensus(rating="STRONG_SELL"))
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    assert raw.business_outlook.value == AIDimensionValue.NEUTRAL  # (+2 and -2) averages to 0
    assert any("business_outlook" in note for note in raw.contradictions)


def test_deterministic_provider_fundamental_only_evidence_still_yields_review_below_minimum(monkeypatch=None):
    """Fundamental-only evidence, but too few categories present to clear
    the minimum-evidence gate -- the overall assessment (not the raw
    per-dimension output) must land on REVIEW."""
    categories = {"business_quality": _FakeCategory("Business Quality", 95.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(categories=categories)
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    coverage = build_evidence_coverage(fundamental_coverage=1 / 8, analyst_consensus=None, technical_summary=None)
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=evidence, evidence_coverage=coverage,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == AIDimensionValue.REVIEW
    assert assessment.score is None


def test_deterministic_provider_analyst_only_evidence_yields_review_when_insufficient():
    """Analyst Consensus alone (no fundamentals, no technicals) only
    populates business_outlook and valuation_context -- two of six
    dimensions, below AI_MINIMUM_ASSESSABLE_DIMENSIONS -- so the overall
    result must be REVIEW even though those two look confident."""
    evidence = build_evidence_payload(categories={}, analyst_consensus=_FakeConsensus(rating="STRONG_BUY", upside_to_mean=0.30))
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    coverage = build_evidence_coverage(fundamental_coverage=0.0, analyst_consensus=_FakeConsensus(coverage=1.0), technical_summary=None)
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=evidence, evidence_coverage=coverage,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == AIDimensionValue.REVIEW
    assert assessment.score is None


def test_deterministic_provider_technical_only_evidence_yields_review_when_insufficient():
    """Technical Summary alone only populates catalyst_strength -- one of
    six dimensions -- so the overall result must be REVIEW."""
    evidence = build_evidence_payload(categories={}, technical_summary=_FakeTechnical(overall="STRONG_BUY"))
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    coverage = build_evidence_coverage(fundamental_coverage=0.0, analyst_consensus=None, technical_summary=_FakeTechnical(coverage=1.0))
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=evidence, evidence_coverage=coverage,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == AIDimensionValue.REVIEW
    assert assessment.score is None


def test_deterministic_provider_all_domains_available_produces_a_valid_synthesis():
    categories = {
        "business_quality": _FakeCategory("Business Quality", 80.0, 1.0, "AVAILABLE"),
        "earnings_growth": _FakeCategory("Earnings Growth", 70.0, 1.0, "AVAILABLE"),
        "valuation": _FakeCategory("Valuation", 60.0, 1.0, "AVAILABLE"),
        "financial_strength": _FakeCategory("Financial Strength", 75.0, 1.0, "AVAILABLE"),
        "momentum": _FakeCategory("Momentum", 65.0, 1.0, "AVAILABLE"),
    }
    analyst = _FakeConsensus(rating="BUY", coverage=1.0)
    technical = _FakeTechnical(overall="BUY", coverage=1.0)
    evidence = build_evidence_payload(categories=categories, analyst_consensus=analyst, technical_summary=technical)
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    coverage = build_evidence_coverage(fundamental_coverage=1.0, analyst_consensus=analyst, technical_summary=technical)
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=evidence, evidence_coverage=coverage,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating != AIDimensionValue.REVIEW
    assert assessment.score is not None
    assert assessment.confidence > 0.5
