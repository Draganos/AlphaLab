"""Deterministic, offline tests for
alpha_lab.research_state_ai_evidence.build_evidence_payload_from_research_state
(roadmap Phase 6): the adapter that widens the AI Research Rating's evidence
boundary to Macro/Donatien/Alignment/Ethics/News, sourced from
ResearchState, while leaving StockResearch-derived evidence identical to
what the pre-existing `build_evidence_payload` already produces.

Also carries the regression test the Phase 6 design review explicitly
required: running this adapter against a ResearchState with no
macro/donatien/alignment/ethics/news evidence must reproduce the exact same
AIResearchAssessment fields the pre-Phase-6 call site already produces,
except for the newly added fields."""

from datetime import UTC, date, datetime

from alpha_lab.research import CATEGORY_LABELS, CATEGORY_ORDER
from alpha_lab.research.ai_rating import (
    DeterministicAIRatingProvider,
    build_ai_research_assessment,
    build_evidence_coverage,
    build_evidence_payload,
)
from alpha_lab.research.model import CategoryResult, CategoryStatus, ConfidenceBreakdown, StockResearch
from alpha_lab.research_state import ResearchField, ResearchState
from alpha_lab.research_state_ai_evidence import build_evidence_payload_from_research_state


def _stub_research() -> StockResearch:
    categories = {
        name: CategoryResult(
            name=name, label=CATEGORY_LABELS[name],
            score=75.0 if name == "business_quality" else None,
            coverage=1.0 if name == "business_quality" else 0.0,
            status=CategoryStatus.AVAILABLE if name == "business_quality" else CategoryStatus.UNAVAILABLE,
            metrics=[], evidence=[], unavailable_metrics=[], sources=[],
        )
        for name in CATEGORY_ORDER
    }
    return StockResearch(
        ticker="NVDA", company_name="NVIDIA", sector=None, industry=None, security_type=None,
        categories=categories, overall_score=75.0, overall_coverage=1.0,
        confidence=5.0, confidence_label="Moderate confidence", score_interpretation="Positive",
        confidence_breakdown=ConfidenceBreakdown(
            overall_coverage=1.0, category_breadth=0.1, freshness=1.0,
            source_quality=1.0, data_quality_penalty_applied=False,
        ),
        strengths=[], weaknesses=[], risks=[], catalysts=[], sources=[],
        data_quality_status="valid", rating_version="v1", configuration_hash="cfg",
        evaluation_date=date.today(), generated_at=datetime.now(UTC),
    )


def _not_computed() -> ResearchField:
    return ResearchField(status="NOT_COMPUTED")


def _minimal_state(**field_overrides) -> ResearchState:
    """A ResearchState with only `stock_research` set and every other
    domain honestly NOT_COMPUTED, unless overridden -- the minimal shape
    the adapter needs."""
    research = _stub_research()
    base = dict(
        ticker=research.ticker, evaluation_date=research.evaluation_date,
        research_refresh_version_id="refresh-v1",
        stock_research=research,
        fundamentals=_not_computed(), analyst_activity=_not_computed(), technicals=_not_computed(),
        news=_not_computed(), news_impact=_not_computed(), macro=_not_computed(),
        donatien=_not_computed(), donatien_alignment=_not_computed(), ethics=_not_computed(),
        deterministic_score=_not_computed(), ai_research=_not_computed(), ai_rating=_not_computed(),
    )
    base.update(field_overrides)
    return ResearchState(**base)


def test_no_extra_domains_reproduces_the_same_items_as_build_evidence_payload():
    """With every non-fundamental domain NOT_COMPUTED, the adapter must
    contribute zero new evidence items -- its output for the
    StockResearch-derived part must be byte-identical to calling the
    existing build_evidence_payload directly."""
    state = _minimal_state()
    direct = build_evidence_payload(categories=state.stock_research.categories)
    via_adapter = build_evidence_payload_from_research_state(state)
    assert [item.model_dump() for item in via_adapter.items] == [item.model_dump() for item in direct]
    assert via_adapter.provenance == {}


def test_macro_evidence_is_added_when_available_with_correct_provenance():
    macro_field = ResearchField(
        value={"regime": "RISK_ON", "regime_score": 0.8, "confidence": 0.9, "coverage": 1.0},
        status="FULL", provenance_id="macro-snap-123",
    )
    state = _minimal_state(macro=macro_field)
    result = build_evidence_payload_from_research_state(state)
    macro_items = [item for item in result.items if item.evidence_id == "macro:regime"]
    assert len(macro_items) == 1
    assert macro_items[0].value == "RISK_ON"
    assert result.provenance["macro:regime"] == "macro-snap-123"


def test_macro_evidence_omitted_when_not_computed():
    result = build_evidence_payload_from_research_state(_minimal_state())
    assert all(item.evidence_id != "macro:regime" for item in result.items)
    assert "macro:regime" not in result.provenance


def test_donatien_and_alignment_evidence_added_with_provenance():
    donatien_field = ResearchField(
        value={"dominant_regime": "GROWTH", "scenario_weights": {"GROWTH": 0.6}},
        status="FULL", provenance_id="donatien-snap-1",
    )
    alignment_field = ResearchField(
        value={"alignment": "ALIGNED"}, status="FULL", provenance_id="alignment-snap-1",
    )
    state = _minimal_state(donatien=donatien_field, donatien_alignment=alignment_field)
    result = build_evidence_payload_from_research_state(state)
    ids = {item.evidence_id: item for item in result.items}
    assert ids["donatien:dominant_regime"].value == "GROWTH"
    assert result.provenance["donatien:dominant_regime"] == "donatien-snap-1"
    assert ids["alignment:overall"].value == "ALIGNED"
    assert result.provenance["alignment:overall"] == "alignment-snap-1"


def test_ethics_evidence_added_with_provenance():
    ethics_field = ResearchField(
        value={"ethical_status": "REVIEW", "business_tags": [], "exclusion_reasons": [], "review_reasons": ["x"]},
        status="FULL", provenance_id="ethics-row-1",
    )
    state = _minimal_state(ethics=ethics_field)
    result = build_evidence_payload_from_research_state(state)
    ethics_items = [item for item in result.items if item.evidence_id == "ethics:status"]
    assert len(ethics_items) == 1
    assert ethics_items[0].value == "REVIEW"
    assert result.provenance["ethics:status"] == "ethics-row-1"


def test_news_impact_evidence_only_added_when_something_was_actually_classified():
    unclassified_field = ResearchField(
        value=[{"content_hash": "h1", "tags": [], "is_classified": False, "methodology_version": "news-impact-v1"}],
        status="NO_EVIDENCE",
    )
    state = _minimal_state(news_impact=unclassified_field)
    result = build_evidence_payload_from_research_state(state)
    assert all(item.evidence_id != "news:impact_categories" for item in result.items)

    classified_field = ResearchField(
        value=[
            {
                "content_hash": "h2",
                "tags": [{"category": "EARNINGS_GUIDANCE", "matched_phrases": ["earnings"], "related_domains": ["fundamentals"]}],
                "is_classified": True,
                "methodology_version": "news-impact-v1",
            }
        ],
        status="FULL", provenance_id="news-impact-1",
    )
    state = _minimal_state(news_impact=classified_field)
    result = build_evidence_payload_from_research_state(state)
    news_items = [item for item in result.items if item.evidence_id == "news:impact_categories"]
    assert len(news_items) == 1
    assert "EARNINGS_GUIDANCE" in news_items[0].description
    assert result.provenance["news:impact_categories"] == "news-impact-1"


def test_regression_unchanged_state_reproduces_old_assessment_except_new_fields():
    """The design review's explicit regression requirement: with no new
    evidence domains present, the resulting AIResearchAssessment must match
    the pre-Phase-6 call site's result field-for-field, except the newly
    added fields (thesis/invalidation_conditions/material_changes/
    research_refresh_version_id/evidence_provenance)."""
    research = _stub_research()
    state = _minimal_state()

    old_evidence = build_evidence_payload(categories=research.categories)
    old_coverage = build_evidence_coverage(fundamental_coverage=research.overall_coverage)
    old_provider = DeterministicAIRatingProvider()
    old_raw = old_provider.assess(research.ticker, old_evidence)
    old_assessment = build_ai_research_assessment(
        ticker=research.ticker, raw=old_raw, evidence=old_evidence, evidence_coverage=old_coverage,
        research_schema_version="stockresearch-v2", as_of=research.evaluation_date,
        generated_at=datetime.now(UTC),
    )

    new_payload = build_evidence_payload_from_research_state(state)
    new_coverage = build_evidence_coverage(fundamental_coverage=research.overall_coverage)
    new_provider = DeterministicAIRatingProvider()
    new_raw = new_provider.assess(research.ticker, new_payload.items)
    new_assessment = build_ai_research_assessment(
        ticker=research.ticker, raw=new_raw, evidence=new_payload.items, evidence_coverage=new_coverage,
        research_schema_version="stockresearch-v2", as_of=research.evaluation_date,
        generated_at=datetime.now(UTC),
        research_refresh_version_id=state.research_refresh_version_id,
        evidence_provenance=new_payload.provenance,
    )

    assert new_assessment.score == old_assessment.score
    assert new_assessment.rating == old_assessment.rating
    assert new_assessment.confidence == old_assessment.confidence
    assert new_assessment.dimensions == old_assessment.dimensions
    assert new_assessment.positives == old_assessment.positives
    assert new_assessment.risks == old_assessment.risks
    assert new_assessment.catalysts == old_assessment.catalysts
    assert new_assessment.contradictions == old_assessment.contradictions
    assert new_assessment.evidence_gaps == old_assessment.evidence_gaps
    assert new_assessment.supporting_evidence == old_assessment.supporting_evidence
    assert new_assessment.evidence_coverage == old_assessment.evidence_coverage
    # The explicitly-added fields are the only ones allowed to differ.
    assert new_assessment.research_refresh_version_id == "refresh-v1"
    assert old_assessment.research_refresh_version_id is None
