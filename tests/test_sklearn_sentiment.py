"""Tests for SklearnFinancialSentimentProvider's wiring: sentence
extraction, composition with RuleBasedFinancialResearchProvider, evidence
construction, and provenance fields. Uses a tiny toy pipeline fit on a
handful of in-memory sentences (not Financial PhraseBank) so these tests
never download the real dataset or run the real training script -- only
`scripts/train_sentiment_classifier.py`, run explicitly and separately,
touches the real data."""

import joblib
import pytest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from alpha_lab.ai.sklearn_sentiment import (
    DEFAULT_MODEL_PATH,
    SklearnFinancialSentimentProvider,
    _extract_sentences,
)


def _document(doc_id: int, text: str) -> dict:
    return {"id": doc_id, "text": text, "title": "T", "source": "https://x", "document_date": "2026-01-01"}


@pytest.fixture
def toy_model_path(tmp_path):
    """A real (tiny, toy) sklearn Pipeline -- exercises the actual
    joblib load / predict / predict_proba code path without requiring the
    real Financial PhraseBank dataset or the real trained model."""
    texts = [
        "strong demand exceeded expectations",
        "record revenue this quarter",
        "outperformed guidance significantly",
        "weak demand missed expectations",
        "disappointing results this quarter",
        "underperformed relative to guidance",
        "the meeting was held on tuesday",
        "the office is located downtown",
        "quarterly filing was submitted today",
    ]
    labels = (
        ["positive"] * 3 + ["negative"] * 3 + ["neutral"] * 3
    )
    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer()),
        ("classifier", LogisticRegression(max_iter=1000)),
    ])
    pipeline.fit(texts, labels)
    model_path = tmp_path / "toy_model.joblib"
    joblib.dump(
        {
            "pipeline": pipeline,
            "metadata": {"dataset_subset": "Sentences_75Agree.txt", "test_accuracy": 1.0},
        },
        model_path,
    )
    return model_path


def test_missing_model_file_raises_file_not_found_error(tmp_path):
    missing_path = tmp_path / "does-not-exist.joblib"
    with pytest.raises(FileNotFoundError, match="train_sentiment_classifier"):
        SklearnFinancialSentimentProvider(model_path=missing_path)


def test_default_model_path_points_at_gitignored_data_models_directory():
    assert str(DEFAULT_MODEL_PATH) == "data/models/financial_sentiment_classifier.joblib"


def test_extract_sentences_splits_on_sentence_boundaries():
    documents = [_document(
        1, "Strong demand continued. Margins improved this quarter. We remain optimistic about growth."
    )]
    sentences = _extract_sentences(documents)
    texts = [text for text, _doc_id, _excerpt in sentences]
    assert texts == [
        "Strong demand continued.",
        "Margins improved this quarter.",
        "We remain optimistic about growth.",
    ]


def test_extract_sentences_skips_short_fragments():
    documents = [_document(1, "Item 1. Strong demand continued across every reported product segment this year.")]
    sentences = _extract_sentences(documents)
    assert all(len(text) >= 20 for text, _doc_id, _excerpt in sentences)


def test_extract_sentences_skips_documents_without_an_id():
    documents = [{"text": "Strong demand continued across every reported product segment."}]
    assert _extract_sentences(documents) == []


def test_analyze_delegates_non_sentiment_dimensions_to_rule_based(toy_model_path):
    provider = SklearnFinancialSentimentProvider(model_path=toy_model_path)
    text = "We raised guidance for the full year given strong demand."
    result = provider.analyze("NVDA", [_document(1, text)])
    assert result.guidance_score > 0  # rule-based lexicon still drives this dimension
    assert result.provider == "SklearnFinancialSentimentProvider"


def test_analyze_returns_valid_result_with_no_documents(toy_model_path):
    provider = SklearnFinancialSentimentProvider(model_path=toy_model_path)
    result = provider.analyze("NVDA", [])
    assert result.sentiment_score == 0
    assert result.provider == "SklearnFinancialSentimentProvider"


def test_analyze_evidence_excerpts_are_verbatim_sentences(toy_model_path):
    provider = SklearnFinancialSentimentProvider(model_path=toy_model_path)
    text = "Some preamble sentence here. Strong demand exceeded expectations this quarter for our flagship segment."
    result = provider.analyze("NVDA", [_document(9, text)])
    for reference in result.evidence:
        assert reference.document_id in (9,)
        assert reference.excerpt in text


def test_analyze_model_field_records_dataset_subset(toy_model_path):
    provider = SklearnFinancialSentimentProvider(model_path=toy_model_path)
    result = provider.analyze("NVDA", [_document(1, "Strong demand exceeded expectations this quarter overall.")])
    assert "Sentences_75Agree.txt" in result.model


def test_analyze_is_deterministic_for_identical_input(toy_model_path):
    provider = SklearnFinancialSentimentProvider(model_path=toy_model_path)
    documents = [_document(1, "Strong demand exceeded expectations broadly across every reported segment.")]
    first = provider.analyze("NVDA", documents)
    second = provider.analyze("NVDA", documents)
    assert first.model_dump(exclude={"analysis_date"}) == second.model_dump(exclude={"analysis_date"})
