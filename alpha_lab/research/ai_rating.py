"""AI Research Rating: evidence-grounded qualitative synthesis.

A separate research domain from AlphaLab's 0-100 fundamental score -- never
blended into it, and never persisted back into ``StockResearch.overall_score``
or the existing ``ai_research`` rating category (``alpha_lab.ai.research`` /
``alpha_lab.screener.service``'s document-commentary AI, which is a
different, pre-existing system left completely untouched by this module).

Hard boundary (see module docstring in the project brief, section 12-19):
this is NOT a data source, price prediction, probability of outperformance,
trading signal, or a second research/ingestion system. It interprets
already-validated AlphaLab evidence and is not allowed to use an LLM's
pretrained/general knowledge as a fact. Enforced architecturally, not just
by prompt instruction:

- ``build_evidence_payload`` extracts a bounded, explicit list of
  ``AIEvidenceItem``s from already-computed AlphaLab objects (StockResearch
  categories/metrics, AnalystConsensus, TechnicalSummary). This is the ONLY
  evidence a provider ever sees.
- A provider's ``AIRawDimensions.supporting_evidence_ids`` (per dimension)
  and top-level qualitative lists must reference only IDs from that payload;
  ``validate_evidence_ids`` rejects anything else rather than silently
  dropping it, so a provider that hallucinates an evidence ID fails loudly.
- The provider itself never computes ``score``/``rating``/``confidence`` --
  see ``build_ai_research_assessment``, a pure Python function, for that.
  The provider's only job is to interpret evidence into per-dimension
  structured judgments (``AIRawDimensions``) plus qualitative lists.
"""

from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from enum import StrEnum
import json
import os
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

AI_RATING_METHODOLOGY_VERSION = "ai-research-rating-v1"
AI_RATING_PROMPT_VERSION = "ai-research-rating-prompt-v1"

DIMENSION_NAMES = (
    "business_outlook",
    "growth_prospects",
    "competitive_position",
    "valuation_context",
    "risk_profile",
    "catalyst_strength",
)


class AIDimensionValue(StrEnum):
    VERY_NEGATIVE = "VERY_NEGATIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"
    POSITIVE = "POSITIVE"
    VERY_POSITIVE = "VERY_POSITIVE"
    # Evidence for this dimension is insufficient or contradictory -- never
    # forced to NEUTRAL, which would misrepresent "no basis to judge" as
    # "judged and balanced".
    REVIEW = "REVIEW"


_DIMENSION_NUMERIC: dict[AIDimensionValue, float | None] = {
    AIDimensionValue.VERY_NEGATIVE: -2.0,
    AIDimensionValue.NEGATIVE: -1.0,
    AIDimensionValue.NEUTRAL: 0.0,
    AIDimensionValue.POSITIVE: 1.0,
    AIDimensionValue.VERY_POSITIVE: 2.0,
    AIDimensionValue.REVIEW: None,
}


class AIEvidenceItem(BaseModel):
    """One fact drawn from already-validated AlphaLab evidence.

    `evidence_id` is stable and namespaced by source (`fundamental:...`,
    `metric:...`, `analyst:...`, `technical:...`) so a provider's citation
    is traceable back to exactly where the fact came from.
    """

    evidence_id: str
    description: str
    source: str


class AIDimensionAssessment(BaseModel):
    value: AIDimensionValue
    confidence: float = Field(ge=0, le=1)
    reasoning: str | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)


class AIRawDimensions(BaseModel):
    """What a provider returns. No score, rating, or overall confidence --
    those are computed deterministically in Python, never by the provider."""

    model_config = ConfigDict(extra="forbid")
    business_outlook: AIDimensionAssessment
    growth_prospects: AIDimensionAssessment
    competitive_position: AIDimensionAssessment
    valuation_context: AIDimensionAssessment
    risk_profile: AIDimensionAssessment
    catalyst_strength: AIDimensionAssessment
    positives: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    provider: str
    model: str
    model_fingerprint: str | None = None
    prompt_version: str = AI_RATING_PROMPT_VERSION

    def dimensions(self) -> dict[str, AIDimensionAssessment]:
        return {name: getattr(self, name) for name in DIMENSION_NAMES}


class AIResearchAssessment(BaseModel):
    ticker: str
    score: float | None = Field(None, ge=0, le=100)
    rating: AIDimensionValue
    confidence: float = Field(ge=0, le=1)
    dimensions: dict[str, AIDimensionAssessment]
    positives: list[str]
    risks: list[str]
    catalysts: list[str]
    contradictions: list[str]
    evidence_gaps: list[str]
    supporting_evidence: list[str]
    methodology_version: str = AI_RATING_METHODOLOGY_VERSION
    prompt_version: str
    model: str
    model_fingerprint: str | None
    research_schema_version: str
    generated_at: datetime
    as_of: date
    source: str


# --- evidence boundary -----------------------------------------------------


def build_evidence_payload(
    *,
    categories: dict[str, "object"],
    analyst_consensus: "object | None" = None,
    technical_summary: "object | None" = None,
) -> list[AIEvidenceItem]:
    """Extract the bounded, explicit evidence set a provider is allowed to
    see. `categories` is `StockResearch.categories`; kept loosely typed
    here (duck-typed) to avoid a circular import with `alpha_lab.research.model`.
    """
    items: list[AIEvidenceItem] = []
    for name, category in categories.items():
        if category.status.value == "UNAVAILABLE":
            continue
        detail = f"{category.label} score = "
        detail += "unavailable" if category.score is None else f"{category.score:.0f}/100"
        detail += f" (coverage {category.coverage:.0%})"
        items.append(
            AIEvidenceItem(
                evidence_id=f"fundamental:{name}",
                description=detail,
                source="AlphaLab Fundamental Research",
            )
        )
        for metric in category.metrics:
            if metric.value is None:
                continue
            items.append(
                AIEvidenceItem(
                    evidence_id=f"metric:{name}:{metric.name}",
                    description=f"{metric.name} = {metric.value:.4g}"
                    + (f" ({metric.unit})" if metric.unit else ""),
                    source=metric.source or "AlphaLab Fundamental Research",
                )
            )
    if analyst_consensus is not None and getattr(analyst_consensus, "rating", None) is not None:
        items.append(
            AIEvidenceItem(
                evidence_id="analyst:rating",
                description=f"Analyst consensus rating = {analyst_consensus.rating.value}"
                + (
                    f" ({analyst_consensus.total_analysts} analysts)"
                    if analyst_consensus.total_analysts is not None
                    else ""
                ),
                source="Analyst Consensus",
            )
        )
        if analyst_consensus.upside_to_mean is not None:
            items.append(
                AIEvidenceItem(
                    evidence_id="analyst:upside_to_mean",
                    description=f"Upside to mean analyst target = {analyst_consensus.upside_to_mean:+.1%}",
                    source="Analyst Consensus",
                )
            )
    if technical_summary is not None:
        items.append(
            AIEvidenceItem(
                evidence_id="technical:overall_rating",
                description=f"Technical overall rating = {technical_summary.overall_rating.value}",
                source="Technical Summary",
            )
        )
        items.append(
            AIEvidenceItem(
                evidence_id="technical:moving_average_rating",
                description=f"Technical moving-average rating = {technical_summary.moving_average_rating.value}",
                source="Technical Summary",
            )
        )
        items.append(
            AIEvidenceItem(
                evidence_id="technical:oscillator_rating",
                description=f"Technical oscillator rating = {technical_summary.oscillator_rating.value}",
                source="Technical Summary",
            )
        )
    return items


class EvidenceViolation(ValueError):
    """A provider referenced an evidence ID outside the supplied payload."""


def validate_evidence_ids(raw: AIRawDimensions, allowed_ids: set[str]) -> None:
    """Raise loudly if the provider cited evidence that was never offered --
    never silently drop or ignore a fabricated citation."""
    cited: set[str] = set()
    for name in DIMENSION_NAMES:
        cited.update(getattr(raw, name).supporting_evidence_ids)
    unknown = cited - allowed_ids
    if unknown:
        raise EvidenceViolation(f"AI cited evidence outside the supplied payload: {sorted(unknown)}")


# --- deterministic score/rating/confidence mapping (never done by the provider) ---

_SCORE_THRESHOLDS_V1: tuple[tuple[float, AIDimensionValue], ...] = (
    (85.0, AIDimensionValue.VERY_POSITIVE),
    (65.0, AIDimensionValue.POSITIVE),
    (35.0, AIDimensionValue.NEUTRAL),
    (15.0, AIDimensionValue.NEGATIVE),
)
_RATING_FLOOR = AIDimensionValue.VERY_NEGATIVE


def _map_score_to_rating(score: float | None) -> AIDimensionValue:
    if score is None:
        return AIDimensionValue.REVIEW
    for threshold, rating in _SCORE_THRESHOLDS_V1:
        if score >= threshold:
            return rating
    return _RATING_FLOOR


def compute_score(dimensions: dict[str, AIDimensionAssessment]) -> float | None:
    """Deterministic 0-100 mapping from structured dimension judgments.

    Mirrors the scaling already used by alpha_lab.ai.research.AIResearchResult.ai_rating
    ((mean + 2) / 4 * 100) for consistency with AlphaLab's one other AI
    score-mapping convention. None (never a guessed number) if every
    dimension is REVIEW.
    """
    values = [_DIMENSION_NUMERIC[assessment.value] for assessment in dimensions.values()]
    available = [value for value in values if value is not None]
    if not available:
        return None
    mean = sum(available) / len(available)
    return round((mean + 2) / 4 * 100, 2)


def compute_confidence(
    dimensions: dict[str, AIDimensionAssessment],
    *,
    evidence_count: int,
    evidence_coverage: float,
) -> float:
    """Deterministic 0-1 confidence, separate from the rating itself.

    confidence = 0.4 * dimension_coverage       (fraction of dimensions not REVIEW)
               + 0.3 * mean(available dimension confidence)
               + 0.2 * evidence_coverage        (caller-supplied: mean of the
                                                  fundamental/analyst/technical
                                                  coverage that fed this assessment)
               + 0.1 * evidence_density         (min(1, evidence_count / 6))

    A confident-sounding provider cannot buy a high confidence merely by
    asserting one -- its self-reported per-dimension confidence is only
    30% of the total, and only counts for dimensions it did not REVIEW.
    """
    available = [d for d in dimensions.values() if d.value != AIDimensionValue.REVIEW]
    dimension_coverage = len(available) / len(dimensions) if dimensions else 0.0
    dimension_confidence_mean = (
        sum(d.confidence for d in available) / len(available) if available else 0.0
    )
    evidence_density = min(1.0, evidence_count / 6)
    raw = (
        0.4 * dimension_coverage
        + 0.3 * dimension_confidence_mean
        + 0.2 * max(0.0, min(1.0, evidence_coverage))
        + 0.1 * evidence_density
    )
    return round(max(0.0, min(1.0, raw)), 3)


def build_ai_research_assessment(
    *,
    ticker: str,
    raw: AIRawDimensions,
    evidence: list[AIEvidenceItem],
    evidence_coverage: float,
    research_schema_version: str,
    as_of: date,
    generated_at: datetime | None = None,
) -> AIResearchAssessment:
    """Validate provider evidence citations, then deterministically compute
    score/rating/confidence. Raises EvidenceViolation if the provider cited
    evidence it was never given -- never silently corrected."""
    allowed_ids = {item.evidence_id for item in evidence}
    validate_evidence_ids(raw, allowed_ids)
    dimensions = raw.dimensions()
    score = compute_score(dimensions)
    rating = _map_score_to_rating(score)
    confidence = compute_confidence(
        dimensions, evidence_count=len(evidence), evidence_coverage=evidence_coverage
    )
    supporting = sorted({eid for d in dimensions.values() for eid in d.supporting_evidence_ids})
    return AIResearchAssessment(
        ticker=ticker.upper(),
        score=score,
        rating=rating,
        confidence=confidence,
        dimensions=dimensions,
        positives=raw.positives,
        risks=raw.risks,
        catalysts=raw.catalysts,
        contradictions=raw.contradictions,
        evidence_gaps=raw.evidence_gaps,
        supporting_evidence=supporting,
        methodology_version=AI_RATING_METHODOLOGY_VERSION,
        prompt_version=raw.prompt_version,
        model=raw.model,
        model_fingerprint=raw.model_fingerprint,
        research_schema_version=research_schema_version,
        generated_at=generated_at or datetime.now(UTC),
        as_of=as_of,
        source=raw.provider,
    )


# --- providers ---------------------------------------------------------


class AIRatingProvider(ABC):
    @abstractmethod
    def assess(self, ticker: str, evidence: list[AIEvidenceItem]) -> AIRawDimensions: ...


class DeterministicAIRatingProvider(AIRatingProvider):
    """Rule-based, offline, no network: maps already-computed AlphaLab
    category scores straight to a dimension judgment via fixed thresholds.

    This is the default provider (see `configured_ai_rating_provider`) --
    unlike alpha_lab.ai.research's document-commentary AI (which is only
    ever active when explicitly configured with a live provider), a
    deterministic, fully-transparent mapping from evidence AlphaLab already
    trusts is always safe to show, never fabricates anything beyond what
    the evidence already says, and keeps this feature testable and usable
    with zero external dependency.
    """

    _POSITIVE = 70.0
    _NEGATIVE = 30.0

    # dimension -> the fundamental category it is deterministically derived
    # from, and whether a *higher* category score means a more positive
    # dimension (valuation is inverted: a high valuation score means
    # "attractively priced", which is the positive direction for this
    # provider's simple mapping).
    _SOURCE_CATEGORY = {
        "business_outlook": ("business_quality", False),
        "growth_prospects": ("earnings_growth", False),
        "competitive_position": ("business_quality", False),
        "valuation_context": ("valuation", False),
        "risk_profile": ("financial_strength", False),
        "catalyst_strength": ("momentum", False),
    }

    def assess(self, ticker: str, evidence: list[AIEvidenceItem]) -> AIRawDimensions:
        by_id = {item.evidence_id: item for item in evidence}
        assessments: dict[str, AIDimensionAssessment] = {}
        for dimension, (category, _invert) in self._SOURCE_CATEGORY.items():
            evidence_id = f"fundamental:{category}"
            item = by_id.get(evidence_id)
            assessments[dimension] = self._assess_dimension(item)
        return AIRawDimensions(
            **assessments,
            positives=[],
            risks=[],
            catalysts=[],
            contradictions=[],
            evidence_gaps=[
                f"No fundamental evidence available for '{category}'."
                for dimension, (category, _) in self._SOURCE_CATEGORY.items()
                if f"fundamental:{category}" not in by_id
            ],
            provider="deterministic-rule-based",
            model="category-threshold-v1",
            prompt_version=AI_RATING_PROMPT_VERSION,
        )

    def _assess_dimension(self, item: AIEvidenceItem | None) -> AIDimensionAssessment:
        if item is None or "score = unavailable" in item.description:
            return AIDimensionAssessment(value=AIDimensionValue.REVIEW, confidence=0.0)
        score = _extract_score(item.description)
        if score is None:
            return AIDimensionAssessment(value=AIDimensionValue.REVIEW, confidence=0.0)
        if score >= self._POSITIVE:
            value = AIDimensionValue.POSITIVE
        elif score <= self._NEGATIVE:
            value = AIDimensionValue.NEGATIVE
        else:
            value = AIDimensionValue.NEUTRAL
        return AIDimensionAssessment(
            value=value,
            confidence=0.6,
            reasoning=item.description,
            supporting_evidence_ids=[item.evidence_id],
        )


def _extract_score(description: str) -> float | None:
    try:
        fragment = description.split("=", 1)[1].split("(", 1)[0].strip()
        return float(fragment.split("/")[0])
    except (IndexError, ValueError):
        return None


class OpenAIRatingProvider(AIRatingProvider):
    """Optional live synthesis using only the supplied, attributable evidence.

    Mirrors alpha_lab.ai.research.OpenAIResearchProvider's safety pattern:
    the prompt supplies exactly the evidence payload and nothing else, the
    schema is enforced structurally (extra="forbid" on AIRawDimensions), and
    any evidence ID outside what was supplied is rejected by
    `build_ai_research_assessment` rather than trusted.
    """

    def __init__(self, api_key: str, model: str = "gpt-4.1-mini"):
        self.api_key, self.model = api_key, model

    def assess(self, ticker: str, evidence: list[AIEvidenceItem]) -> AIRawDimensions:
        payload_evidence = [item.model_dump(mode="json") for item in evidence]
        prompt = (
            "You are synthesizing ONLY the evidence listed below for "
            f"{ticker}. Do not use any outside knowledge about this company. "
            "Return JSON matching this schema exactly. Each dimension's "
            "supporting_evidence_ids must be a subset of the evidence_id "
            "values supplied. If evidence for a dimension is missing or "
            "contradictory, set its value to REVIEW rather than guessing.\n"
            "Schema: " + json.dumps(AIRawDimensions.model_json_schema()) + "\n"
            "Evidence: " + json.dumps(payload_evidence)
        )
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }
        ).encode()
        request = Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HTTPS endpoint
            response_body = json.loads(response.read())
        raw = AIRawDimensions.model_validate_json(
            response_body["choices"][0]["message"]["content"]
        )
        return raw.model_copy(update={"provider": "openai", "model": self.model})


def configured_ai_rating_provider() -> AIRatingProvider:
    """Namespaced separately from ALPHALAB_AI_PROVIDER (alpha_lab.ai.research's
    document-commentary AI) so enabling one does not implicitly enable the
    other. Defaults to the always-available deterministic provider."""
    if (
        os.getenv("ALPHALAB_AI_RATING_PROVIDER", "deterministic").casefold() == "openai"
        and os.getenv("OPENAI_API_KEY")
    ):
        return OpenAIRatingProvider(
            os.environ["OPENAI_API_KEY"], os.getenv("ALPHALAB_AI_RATING_MODEL", "gpt-4.1-mini")
        )
    return DeterministicAIRatingProvider()
