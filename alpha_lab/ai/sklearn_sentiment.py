"""A real, trained sklearn text classifier for `sentiment_score` ONLY,
composed with `RuleBasedFinancialResearchProvider`'s existing phrase-lexicon
logic for the other eight `AIResearchResult` dimensions.

Why hybrid, not a full 9-dimension classifier: the only real labeled
dataset this is trained on -- Financial PhraseBank (see
`alpha_lab.ai.phrasebank`) -- supervises one thing, general positive/
negative/neutral sentiment of a financial sentence. It has no labels for
guidance direction, demand, margin outlook, competitive position,
management confidence, balance-sheet health, risk severity, or catalysts.
Applying a sentiment-only classifier's output to those other eight
dimensions would be false precision: a confident-looking number the
dataset never actually taught the model to produce. `sentiment_score` is
the one dimension where a real trained classifier is honestly applicable;
the rest stay on the same reviewable phrase lexicon every other dimension
already uses (see `alpha_lab.ai.rule_based`'s own module docstring for why
that lexicon approach was chosen there).

Never the default provider (see `configured_ai_research_provider`) --
gated behind explicit `ALPHALAB_AI_PROVIDER=sklearn` opt-in, the same
"prove it before it's the default" precedent `_SCORING_ELIGIBLE_AI_
PROVIDERS` already applies to `RuleBasedFinancialResearchProvider` itself
(see `alpha_lab.screener.service`'s module-level comment): this
classifier's own sentiment component has only been validated against
Financial PhraseBank's held-out test set so far, not yet calibrated
against real AlphaLab forward returns the way a scoring input should be.
"""

from pathlib import Path
from typing import Any
import re

from alpha_lab.ai.research import (
    AIResearchProvider,
    AIResearchResult,
    EvidenceReference,
    _reject_price_target,
)
from alpha_lab.ai.rule_based import RuleBasedFinancialResearchProvider, _to_evidence

DEFAULT_MODEL_PATH = Path("data/models/financial_sentiment_classifier.joblib")

# Dependency-free sentence splitter: break after sentence-ending
# punctuation followed by whitespace and a capital letter/quote -- the same
# kind of deliberately modest, reviewable heuristic
# `RuleBasedFinancialResearchProvider`'s own lexicon uses, not a full NLP
# sentence tokenizer (spaCy/nltk are not project dependencies).
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z"“])')
_MIN_SENTENCE_CHARS = 20  # skips headers/fragments too short to carry real sentiment

# Bounds how many sentences one `analyze()` call classifies -- some real
# 10-Ks run past a million characters (see `MAX_DOCUMENT_TEXT_CHARS`); this
# keeps TF-IDF inference bounded without changing which *sentiment_score*
# convention is used. A deliberately round, documented cap, not tuned.
_MAX_SENTENCES_PER_ANALYSIS = 800

# Same clipping convention `RuleBasedFinancialResearchProvider` uses for
# every one of its own lexicon dimensions
# (`max(-2.0, min(2.0, positive_hits - negative_hits))`) -- kept identical
# here so `sentiment_score` stays on the same scale as the other eight
# dimensions this provider still delegates to that lexicon.
_SCORE_CLAMP = 2.0

# A sentence only counts as a positive/negative "hit" once the classifier's
# own predicted probability clears this bar -- mirrors
# `RuleBasedFinancialResearchProvider`'s implicit all-or-nothing phrase
# match, applied to a probabilistic classifier instead. Round, documented,
# not tuned against AlphaLab's own downstream calibration.
_CONFIDENCE_THRESHOLD = 0.6

_EXCERPT_RADIUS = 90  # matches alpha_lab.ai.rule_based's own excerpt window


class SklearnFinancialSentimentProvider(AIResearchProvider):
    """Real TF-IDF + logistic-regression classifier (trained by
    `scripts/train_sentiment_classifier.py` on Financial PhraseBank) for
    `sentiment_score`; every other `AIResearchResult` field is produced by
    an internally composed `RuleBasedFinancialResearchProvider`, never
    duplicated."""

    provider_name = "SklearnFinancialSentimentProvider"

    def __init__(self, *, model_path: Path | str = DEFAULT_MODEL_PATH):
        import joblib

        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"No trained sentiment classifier found at {model_path}. "
                "Run `python scripts/train_sentiment_classifier.py` first "
                "to fetch Financial PhraseBank and train the model."
            )
        bundle = joblib.load(model_path)
        self._pipeline = bundle["pipeline"]
        self._metadata = bundle["metadata"]
        self._rule_based = RuleBasedFinancialResearchProvider()

    def analyze(self, ticker: str, documents: list[dict[str, Any]]) -> AIResearchResult:
        base = self._rule_based.analyze(ticker, documents)

        sentences = _extract_sentences(documents)[:_MAX_SENTENCES_PER_ANALYSIS]
        if not sentences:
            return base.model_copy(
                update={
                    "provider": self.provider_name,
                    "model": self._metadata.get("dataset_subset", "financial-sentiment-sklearn-v1"),
                    "prompt_version": "sklearn-sentiment-hybrid-v1",
                    "summary": base.summary + " (No sentences available for real sentiment classification.)",
                }
            )

        texts = [text for text, _document_id, _excerpt in sentences]
        predictions = self._pipeline.predict(texts)
        probabilities = self._pipeline.predict_proba(texts)

        positive_hits: list[tuple[str, int, str]] = []
        negative_hits: list[tuple[str, int, str]] = []
        for (label, document_id, excerpt), prediction, probability_row in zip(
            sentences, predictions, probabilities, strict=True
        ):
            confidence = float(probability_row.max())
            if confidence < _CONFIDENCE_THRESHOLD:
                continue
            if prediction == "positive":
                positive_hits.append((label, document_id, excerpt))
            elif prediction == "negative":
                negative_hits.append((label, document_id, excerpt))

        sentiment_score = max(
            -_SCORE_CLAMP, min(_SCORE_CLAMP, float(len(positive_hits) - len(negative_hits)))
        )
        classifier_evidence = _to_evidence(positive_hits[:2]) + _to_evidence(negative_hits[:2])
        classifier_confidence = round(
            min(1.0, (len(positive_hits) + len(negative_hits)) / 10), 4
        )

        key_positives = list(base.key_positives)
        for _label, _document_id, excerpt in positive_hits:
            snippet = _safe_snippet(excerpt)
            if snippet and snippet not in key_positives:
                key_positives.append(snippet)
        key_risks = list(base.key_risks)
        for _label, _document_id, excerpt in negative_hits:
            snippet = _safe_snippet(excerpt)
            if snippet and snippet not in key_risks:
                key_risks.append(snippet)

        return base.model_copy(
            update={
                "sentiment_score": sentiment_score,
                "key_positives": key_positives,
                "key_risks": key_risks,
                "evidence": list(base.evidence) + classifier_evidence,
                "provider": self.provider_name,
                "model": (
                    f"tfidf-logreg-financial-phrasebank-"
                    f"{self._metadata.get('dataset_subset', 'unknown')}"
                ),
                "prompt_version": "sklearn-sentiment-hybrid-v1",
                "confidence": round((base.confidence + classifier_confidence) / 2, 4),
                "summary": (
                    f"Hybrid analysis of {len(documents)} SEC filing(s) for {ticker}: "
                    f"real trained classifier scored {len(sentences)} sentence(s) "
                    f"({len(positive_hits)} positive, {len(negative_hits)} negative at "
                    f">={_CONFIDENCE_THRESHOLD:.0%} confidence) for sentiment_score; "
                    "remaining dimensions from deterministic phrase-lexicon analysis."
                ),
            }
        )


def _safe_snippet(excerpt: str) -> str | None:
    """A real classified sentence, or None if it must be excluded from
    `key_positives`/`key_risks`.

    `analyze()` builds its final `AIResearchResult` via `base.model_copy
    (update=...)`, not a real constructor call -- pydantic's `model_copy`
    deliberately does NOT re-run field validators on `update` values, so
    `AIResearchResult.no_price_targets_in_lists` would silently never fire
    on a classifier-derived snippet the way it does for every other
    `key_positives`/`key_risks` entry in this codebase (confirmed live: a
    price-target-bearing string survives `model_copy` completely
    unvalidated). A real SEC filing sentence discussing an analyst price
    target is a plausible classifier hit, not a hypothetical.

    Filtering here, before the value ever reaches `model_copy`, makes the
    result equivalent to what real validation would have enforced --
    mirroring `alpha_lab.ai.rule_based._to_evidence`'s own precedent of
    skipping one bad excerpt rather than raising and losing the entire
    analysis (every other computed dimension) over a single sentence."""
    try:
        return _reject_price_target(excerpt.strip()) or None
    except ValueError:
        return None


def _extract_sentences(documents: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
    """Every real sentence long enough to carry sentiment, as (sentence,
    document_id, excerpt) -- excerpt is the sentence itself (already a
    verbatim slice of the real document text), reusing
    `alpha_lab.ai.rule_based._to_evidence`'s (phrase, document_id, excerpt)
    tuple shape so evidence construction/price-target rejection is shared,
    not duplicated."""
    results: list[tuple[str, int, str]] = []
    for document in documents:
        document_id = document.get("id")
        if document_id is None:
            continue
        text = str(document.get("text", ""))
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            sentence = sentence.strip()
            if len(sentence) < _MIN_SENTENCE_CHARS:
                continue
            results.append((sentence, int(document_id), sentence))
    return results
