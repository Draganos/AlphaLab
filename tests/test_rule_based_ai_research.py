"""Tests for RuleBasedFinancialResearchProvider: the deterministic,
zero-cost AIResearchProvider that closes the ai_research scoring gap
without any external API. Every assertion checks real phrase-matching
behavior against literal document text -- nothing here is a network or
model-dependent test."""

import pytest

from alpha_lab.ai.research import AIResearchResult
from alpha_lab.ai.rule_based import RuleBasedFinancialResearchProvider


def _document(doc_id: int, text: str) -> dict:
    return {"id": doc_id, "text": text, "title": "T", "source": "https://x", "document_date": "2026-01-01"}


def test_analyze_is_deterministic_for_identical_input():
    provider = RuleBasedFinancialResearchProvider()
    documents = [_document(1, "We saw strong demand and margin expansion this quarter.")]
    first = provider.analyze("NVDA", documents)
    second = provider.analyze("NVDA", documents)
    assert first.model_dump(exclude={"analysis_date"}) == second.model_dump(exclude={"analysis_date"})


def test_analyze_detects_positive_demand_phrase():
    provider = RuleBasedFinancialResearchProvider()
    documents = [_document(1, "Strong demand for our products continued in the quarter.")]
    result = provider.analyze("NVDA", documents)
    assert result.demand_score > 0
    assert "strong demand" in result.key_positives


def test_analyze_detects_negative_demand_phrase():
    provider = RuleBasedFinancialResearchProvider()
    documents = [_document(1, "We experienced weak demand across our product lines.")]
    result = provider.analyze("NVDA", documents)
    assert result.demand_score < 0
    assert "weak demand" in result.key_risks


def test_analyze_with_no_matching_phrases_returns_zero_scores_and_low_confidence():
    provider = RuleBasedFinancialResearchProvider()
    documents = [_document(1, "The company sells widgets to customers around the world.")]
    result = provider.analyze("NVDA", documents)
    assert result.demand_score == 0
    assert result.guidance_score == 0
    assert result.confidence == 0.0


def test_analyze_confidence_scales_with_signal_density():
    provider = RuleBasedFinancialResearchProvider()
    sparse = provider.analyze("NVDA", [_document(1, "Strong demand this quarter.")])
    dense_text = " ".join([
        "strong demand", "record demand", "raised guidance", "margin expansion",
        "market share gain", "well-positioned", "strong balance sheet",
        "record revenue", "new product launch", "increased demand",
    ])
    dense = provider.analyze("NVDA", [_document(1, dense_text)])
    assert dense.confidence > sparse.confidence
    assert dense.confidence == 1.0


def test_risk_score_uses_severity_count_not_the_signed_lexicon():
    provider = RuleBasedFinancialResearchProvider()
    documents = [_document(1, "We disclosed a material weakness and are under investigation.")]
    result = provider.analyze("NVDA", documents)
    assert result.risk_score > 0
    assert "material weakness" in result.key_risks


def test_evidence_excerpts_are_verbatim_from_the_source_document():
    provider = RuleBasedFinancialResearchProvider()
    text = "Some preamble text. Strong demand for our flagship product drove results. Some closing text."
    documents = [_document(7, text)]
    result = provider.analyze("NVDA", documents)
    assert result.evidence, "expected at least one evidence reference"
    for reference in result.evidence:
        assert reference.document_id == 7
        assert reference.excerpt in text


def test_analyze_scores_are_bounded_to_the_schemas_range():
    provider = RuleBasedFinancialResearchProvider()
    # Repeat the same positive phrase many times to try to push a score
    # past the AIResearchResult schema's [-2, 2] bound.
    text = "strong demand. " * 20
    documents = [_document(1, text)]
    result = provider.analyze("NVDA", documents)
    assert -2 <= result.demand_score <= 2


def test_analyze_returns_a_valid_ai_research_result_with_no_documents():
    provider = RuleBasedFinancialResearchProvider()
    result = provider.analyze("NVDA", [])
    assert isinstance(result, AIResearchResult)
    assert result.confidence == 0.0


def test_analyze_ignores_documents_without_an_id():
    provider = RuleBasedFinancialResearchProvider()
    documents = [{"text": "Strong demand.", "title": "T", "source": "x", "document_date": "2026-01-01"}]
    result = provider.analyze("NVDA", documents)
    assert result.evidence == []


def test_analyze_survives_an_excerpt_that_would_trip_the_price_target_validator():
    """Regression test for a self-review finding: a real 10-K/10-Q
    risk-factor section discussing analyst price targets anywhere near a
    matched lexicon phrase used to raise inside EvidenceReference's own
    validator, and analyze_documents' blanket exception handler would
    then discard the ENTIRE result -- silently reproducing the exact 0%
    ai_research coverage this module exists to fix. The phrase match must
    still count toward the score; only that one unsafe excerpt is
    dropped from evidence."""
    text = "Even after analysts lowered their price target, we saw strong demand for our products."
    provider = RuleBasedFinancialResearchProvider()
    result = provider.analyze("NVDA", [_document(1, text)])
    assert result.demand_score > 0  # the match still counts toward the score
    for reference in result.evidence:
        assert "price target" not in reference.excerpt.lower()
        assert "target price" not in reference.excerpt.lower()
