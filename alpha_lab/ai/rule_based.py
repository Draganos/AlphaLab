"""A deterministic, zero-cost `AIResearchProvider` -- V1 of the "local
financial NLP" plan recorded in ARCHITECTURE.md's Document Evidence Engine
phase: real filing text, real phrase matches, real excerpts tied to real
document IDs. No API key, no external network call, no model weights, and
no randomness -- the same documents always produce the same result, which
is exactly what makes this reproducible for point-in-time backtesting in
a way a live LLM call never fully is.

This is explicitly a heuristic keyword/phrase classifier, not sentiment
analysis or language understanding -- it is the same kind of lexicon-based
approach `DeterministicAIResearchProvider` already used as a test fixture,
extended from one shared signal into per-dimension lexicons covering all
nine `AIResearchResult` score fields, and confidence tied to how much real
signal was actually found rather than merely how many documents exist.

Never invents a company's own words: every excerpt is a verbatim slice of
`document["text"]`, and `evidence` always cites the real document_id it
came from -- `AIResearchResult`'s own validators reject anything that
looks like a fabricated price target, and `analyze_documents` fails safe
(returns None) if construction ever raises for any reason.
"""

from typing import Any

from alpha_lab.ai.research import AIResearchProvider, AIResearchResult, EvidenceReference

# Per-dimension (positive, negative) phrase lexicons. Deliberately modest
# and reviewable rather than exhaustive -- a V1 heuristic, not a trained
# model; see this module's own docstring and ARCHITECTURE.md for why a
# larger lexicon or a trained classifier is future work, not this pass.
_LEXICON: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "guidance_score": (
        ("raised guidance", "increased guidance", "raised our outlook", "above our prior guidance"),
        ("lowered guidance", "reduced guidance", "withdrew guidance", "below our prior guidance"),
    ),
    "demand_score": (
        ("strong demand", "record demand", "robust demand", "demand remains strong", "increased demand"),
        ("weak demand", "soft demand", "demand deceleration", "declining demand", "reduced demand"),
    ),
    "margin_outlook_score": (
        ("margin expansion", "improved margins", "margin improvement", "higher margins"),
        ("margin pressure", "margin compression", "margin decline", "lower margins", "compressed margins"),
    ),
    "competitive_position_score": (
        ("market share gain", "competitive advantage", "differentiated", "market leadership"),
        ("market share loss", "increased competition", "competitive pressure", "loss of market share"),
    ),
    "management_confidence_score": (
        ("well-positioned", "confident in our", "optimistic about", "positioned for growth"),
        ("challenging environment", "significant uncertainty", "cautious outlook", "difficult environment"),
    ),
    "balance_sheet_commentary_score": (
        ("strong balance sheet", "ample liquidity", "investment grade", "strong liquidity position"),
        ("liquidity risk", "going concern", "covenant violation", "material weakness", "substantial doubt"),
    ),
    "sentiment_score": (
        ("record revenue", "record results", "outperformed", "exceeded expectations"),
        ("underperformed", "missed expectations", "below expectations", "disappointing results"),
    ),
    "catalyst_score": (
        ("new product launch", "expansion into", "strategic partnership", "completed acquisition"),
        ("discontinued", "divestiture", "restructuring plan", "impairment charge"),
    ),
}

# risk_score is a severity count, not a signed lexicon -- AIResearchResult's
# own convention (see `ai_rating`) is that a HIGHER risk_score means more
# risk, the opposite polarity of every other dimension above.
_RISK_PHRASES = (
    "material weakness", "going concern", "substantial doubt", "under investigation",
    "regulatory investigation", "litigation", "impairment charge", "covenant violation",
)

_EXCERPT_RADIUS = 90  # characters of context on each side of a matched phrase


class RuleBasedFinancialResearchProvider(AIResearchProvider):
    """Zero-cost default: phrase-matches real filing text against the
    lexicons above and reports a real `AIResearchResult`, with confidence
    tied to how much real signal was actually found (see `_confidence`) --
    never a confident-looking score from a document containing none of
    these phrases at all."""

    provider_name = "RuleBasedFinancialResearchProvider"

    def analyze(self, ticker: str, documents: list[dict[str, Any]]) -> AIResearchResult:
        scores: dict[str, float] = {}
        evidence: list[EvidenceReference] = []
        key_positives: list[str] = []
        key_risks: list[str] = []
        total_matches = 0

        for field, (positive_phrases, negative_phrases) in _LEXICON.items():
            positive_hits = _find_all(documents, positive_phrases)
            negative_hits = _find_all(documents, negative_phrases)
            total_matches += len(positive_hits) + len(negative_hits)
            scores[field] = max(-2.0, min(2.0, float(len(positive_hits) - len(negative_hits))))
            evidence.extend(_to_evidence(positive_hits[:2]))
            evidence.extend(_to_evidence(negative_hits[:2]))
            for phrase, _document_id, _excerpt in positive_hits:
                if phrase not in key_positives:
                    key_positives.append(phrase)
            for phrase, _document_id, _excerpt in negative_hits:
                if phrase not in key_risks:
                    key_risks.append(phrase)

        risk_hits = _find_all(documents, _RISK_PHRASES)
        total_matches += len(risk_hits)
        risk_score = max(0.0, min(2.0, float(len(risk_hits))))
        evidence.extend(_to_evidence(risk_hits[:2]))
        for phrase, _document_id, _excerpt in risk_hits:
            if phrase not in key_risks:
                key_risks.append(phrase)

        return AIResearchResult(
            guidance_score=scores["guidance_score"],
            demand_score=scores["demand_score"],
            margin_outlook_score=scores["margin_outlook_score"],
            competitive_position_score=scores["competitive_position_score"],
            management_confidence_score=scores["management_confidence_score"],
            balance_sheet_commentary_score=scores["balance_sheet_commentary_score"],
            risk_score=risk_score,
            sentiment_score=scores["sentiment_score"],
            catalyst_score=scores["catalyst_score"],
            key_positives=key_positives,
            key_risks=key_risks,
            evidence=evidence,
            summary=(
                f"Deterministic phrase-based analysis of {len(documents)} SEC filing(s) for "
                f"{ticker}: {total_matches} lexicon phrase match(es) found across all dimensions."
            ),
            provider=self.provider_name,
            model="financial-phrase-lexicon-v1",
            prompt_version="rule-based-v1",
            confidence=_confidence(total_matches),
        )


def _find_all(
    documents: list[dict[str, Any]], phrases: tuple[str, ...]
) -> list[tuple[str, int, str]]:
    """Every real occurrence of any `phrases` member across `documents`, as
    (phrase, document_id, excerpt) -- an excerpt is a verbatim slice of the
    real document text around the match, never generated text."""
    hits: list[tuple[str, int, str]] = []
    for document in documents:
        document_id = document.get("id")
        if document_id is None:
            continue
        text = str(document.get("text", ""))
        lowered = text.lower()
        for phrase in phrases:
            start = 0
            while True:
                index = lowered.find(phrase, start)
                if index == -1:
                    break
                excerpt_start = max(0, index - _EXCERPT_RADIUS)
                excerpt_end = min(len(text), index + len(phrase) + _EXCERPT_RADIUS)
                hits.append((phrase, int(document_id), text[excerpt_start:excerpt_end]))
                start = index + len(phrase)
    return hits


def _to_evidence(hits: list[tuple[str, int, str]]) -> list[EvidenceReference]:
    return [
        EvidenceReference(document_id=document_id, excerpt=excerpt)
        for _phrase, document_id, excerpt in hits
    ]


def _confidence(total_matches: int) -> float:
    """Tied to how much real signal was actually found, not merely how
    many documents were supplied -- a document set containing none of
    these phrases must never be reported as confidently analyzed. 10
    total matches across all nine dimensions and the risk lexicon reaches
    full confidence; this threshold is a deliberately round, documented
    choice, not derived from any calibration study."""
    return round(min(1.0, total_matches / 10), 4)
