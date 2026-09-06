"""Deterministic, offline tests for AI Research Rating: structured output
validation, the evidence boundary (fabricated citations rejected), the
deterministic score/rating/confidence mapping, REVIEW behaviour, and
provenance. No network call is ever made -- DeterministicAIRatingProvider
is rule-based, and OpenAIRatingProvider is never invoked in these tests."""

from datetime import date

import pytest
from pydantic import ValidationError

from alpha_lab.research.ai_rating import (
    AI_RATING_METHODOLOGY_VERSION,
    AIDimensionAssessment,
    AIDimensionValue,
    AIEvidenceItem,
    AIRawDimensions,
    DeterministicAIRatingProvider,
    EvidenceViolation,
    build_ai_research_assessment,
    build_evidence_payload,
    compute_confidence,
    compute_score,
    validate_evidence_ids,
)


def _dimension(value=AIDimensionValue.NEUTRAL, confidence=0.5, evidence_ids=None) -> AIDimensionAssessment:
    return AIDimensionAssessment(
        value=value, confidence=confidence, supporting_evidence_ids=evidence_ids or []
    )


def _raw(**overrides) -> AIRawDimensions:
    base = {
        "business_outlook": _dimension(),
        "growth_prospects": _dimension(),
        "competitive_position": _dimension(),
        "valuation_context": _dimension(),
        "risk_profile": _dimension(),
        "catalyst_strength": _dimension(),
        "provider": "test-provider",
        "model": "test-model",
    }
    base.update(overrides)
    return AIRawDimensions(**base)


# --- structured output validation -----------------------------------------


def test_raw_dimensions_rejects_unknown_fields():
    """extra='forbid' -- a provider cannot smuggle an unstructured field
    (e.g. a free-text 'price_target') past the schema."""
    with pytest.raises(ValidationError):
        AIRawDimensions(
            business_outlook=_dimension(),
            growth_prospects=_dimension(),
            competitive_position=_dimension(),
            valuation_context=_dimension(),
            risk_profile=_dimension(),
            catalyst_strength=_dimension(),
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
            ticker="NVDA", raw=raw, evidence=evidence, evidence_coverage=0.5,
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
    raw = _raw(**{name: _dimension(AIDimensionValue.REVIEW, confidence=0.0) for name in (
        "business_outlook", "growth_prospects", "competitive_position",
        "valuation_context", "risk_profile", "catalyst_strength",
    )})
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=0.0,
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
def test_uniform_dimension_values_map_to_the_matching_rating_band(value, expected_rating):
    raw = _raw(**{name: _dimension(value) for name in (
        "business_outlook", "growth_prospects", "competitive_position",
        "valuation_context", "risk_profile", "catalyst_strength",
    )})
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=1.0,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating == expected_rating


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


def test_positive_rating_can_coexist_with_low_confidence():
    """A company could have AI Rating: Positive, AI Confidence: Low if
    limited evidence happens to lean positive but coverage is poor."""
    raw = _raw(business_outlook=_dimension(AIDimensionValue.VERY_POSITIVE, confidence=0.9))
    for name in ("growth_prospects", "competitive_position", "valuation_context", "risk_profile", "catalyst_strength"):
        raw = raw.model_copy(update={name: _dimension(AIDimensionValue.REVIEW, confidence=0.0)})
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=0.1,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.rating in (AIDimensionValue.POSITIVE, AIDimensionValue.VERY_POSITIVE)
    assert assessment.confidence < 0.5


# --- provenance --------------------------------------------------------


def test_provenance_fields_are_recorded():
    raw = _raw(provider="deterministic-rule-based", model="category-threshold-v1")
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=[], evidence_coverage=0.5,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.source == "deterministic-rule-based"
    assert assessment.model == "category-threshold-v1"
    assert assessment.methodology_version == AI_RATING_METHODOLOGY_VERSION
    assert assessment.research_schema_version == "stockresearch-v2"
    assert assessment.generated_at is not None


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
        ticker="NVDA", raw=raw, evidence=evidence, evidence_coverage=0.5,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.supporting_evidence == ["fundamental:business_quality", "fundamental:valuation"]


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


# --- DeterministicAIRatingProvider: the always-on, offline default ---------


def test_deterministic_provider_derives_positive_dimension_from_high_category_score():
    categories = {"business_quality": _FakeCategory("Business Quality", 80.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(categories=categories)
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    assert raw.business_outlook.value == AIDimensionValue.POSITIVE
    assert raw.business_outlook.supporting_evidence_ids == ["fundamental:business_quality"]


def test_deterministic_provider_derives_negative_dimension_from_low_category_score():
    categories = {"earnings_growth": _FakeCategory("Earnings Growth", 10.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(categories=categories)
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    assert raw.growth_prospects.value == AIDimensionValue.NEGATIVE


def test_deterministic_provider_reviews_dimensions_with_no_evidence_and_reports_the_gap():
    raw = DeterministicAIRatingProvider().assess("NVDA", [])
    assert raw.business_outlook.value == AIDimensionValue.REVIEW
    assert raw.business_outlook.confidence == 0.0
    assert len(raw.evidence_gaps) == 6


def test_deterministic_provider_never_cites_evidence_it_was_not_given():
    categories = {"business_quality": _FakeCategory("Business Quality", 80.0, 1.0, "AVAILABLE")}
    evidence = build_evidence_payload(categories=categories)
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    allowed = {item.evidence_id for item in evidence}
    validate_evidence_ids(raw, allowed)  # must not raise


def test_deterministic_provider_end_to_end_produces_a_valid_assessment():
    categories = {
        "business_quality": _FakeCategory("Business Quality", 80.0, 1.0, "AVAILABLE"),
        "valuation": _FakeCategory("Valuation", 20.0, 1.0, "AVAILABLE"),
    }
    evidence = build_evidence_payload(categories=categories)
    raw = DeterministicAIRatingProvider().assess("NVDA", evidence)
    assessment = build_ai_research_assessment(
        ticker="NVDA", raw=raw, evidence=evidence, evidence_coverage=0.5,
        research_schema_version="stockresearch-v2", as_of=date.today(),
    )
    assert assessment.score is not None
    assert assessment.rating != AIDimensionValue.REVIEW or assessment.score is None
