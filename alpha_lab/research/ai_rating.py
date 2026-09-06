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

AI_RATING_METHODOLOGY_VERSION = "ai-research-rating-v2"
AI_RATING_PROMPT_VERSION = "ai-research-rating-prompt-v1"

# Minimum evidence required before the OVERALL rating may be anything but
# REVIEW -- both conditions must hold. A single available positive/negative
# dimension is not a synthesis; it is one data point wearing a synthesis's
# clothes. `confidence` is computed independently and may still be low and
# nonzero even when the rating itself is forced to REVIEW (evidence "leans"
# somewhere without being enough to act on).
AI_MINIMUM_ASSESSABLE_DIMENSIONS = 3  # of 6 -- at least half must be non-REVIEW
AI_MINIMUM_EVIDENCE_COVERAGE = 0.34  # of overall_ai_evidence_coverage (roughly 1/3 domains)

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

    `value` carries the same fact as `description`, but machine-readable
    (a 0-100 score, an upside fraction, or a rating word like "BUY") --
    used by DeterministicAIRatingProvider so it never has to string-parse
    its own generated text. An LLM provider is free to ignore it and read
    `description` instead; it is not required to be present.
    """

    evidence_id: str
    description: str
    source: str
    value: float | str | None = None


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


class AIEvidenceCoverage(BaseModel):
    """Domain-aware evidence coverage feeding one AI Research Rating.

    Deliberately NOT a mean of whichever domains happen to be present --
    `analyst_coverage`/`technical_coverage` are 0.0 (not excluded from the
    denominator) when that domain was never supplied, so a ticker with
    100% fundamental coverage and no Analyst Consensus/Technical Summary
    reads as roughly a third covered overall, never as 100%. This never
    affects the fundamental score's own coverage figures, which are
    computed and stored entirely separately.
    """

    fundamental_coverage: float = Field(ge=0, le=1)
    analyst_coverage: float = Field(ge=0, le=1)
    technical_coverage: float = Field(ge=0, le=1)
    overall_ai_evidence_coverage: float = Field(ge=0, le=1)


def build_evidence_coverage(
    *,
    fundamental_coverage: float,
    analyst_consensus: "object | None" = None,
    technical_summary: "object | None" = None,
) -> AIEvidenceCoverage:
    """The only place `overall_ai_evidence_coverage` is computed -- always
    an average over exactly three domains, never over however many happen
    to be present."""
    analyst_coverage = analyst_consensus.coverage if analyst_consensus is not None else 0.0
    technical_coverage = technical_summary.coverage if technical_summary is not None else 0.0
    overall = (fundamental_coverage + analyst_coverage + technical_coverage) / 3
    return AIEvidenceCoverage(
        fundamental_coverage=fundamental_coverage,
        analyst_coverage=analyst_coverage,
        technical_coverage=technical_coverage,
        overall_ai_evidence_coverage=overall,
    )


class AIResearchAssessment(BaseModel):
    ticker: str
    score: float | None = Field(None, ge=0, le=100)
    rating: AIDimensionValue
    confidence: float = Field(ge=0, le=1)
    dimensions: dict[str, AIDimensionAssessment]
    evidence_coverage: AIEvidenceCoverage
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


# Excluded from the AI Research Rating's evidence payload entirely -- not
# given zero weight, not present at all. `ai_research` is
# alpha_lab.screener.service's existing document-commentary AI category
# (fed into the fundamental score); feeding an already-AI-derived category
# back into this AI's own evidence would make the new rating partly a
# synthesis of itself. Analyst Revisions, momentum, and every other
# fundamental category are unaffected -- none of them are AI-derived.
_EXCLUDED_CATEGORIES = frozenset({"ai_research"})


def build_evidence_payload(
    *,
    categories: dict[str, "object"],
    analyst_consensus: "object | None" = None,
    technical_summary: "object | None" = None,
) -> list[AIEvidenceItem]:
    """Extract the bounded, explicit evidence set a provider is allowed to
    see. `categories` is `StockResearch.categories`; kept loosely typed
    here (duck-typed) to avoid a circular import with `alpha_lab.research.model`.

    Deliberately excludes `_EXCLUDED_CATEGORIES` (the existing AI-derived
    `ai_research` category) -- this AI Research Rating synthesizes
    fundamental/Analyst Consensus/Technical Summary evidence, never an
    already-AI-derived score, however it was itself computed.
    """
    items: list[AIEvidenceItem] = []
    for name, category in categories.items():
        if name in _EXCLUDED_CATEGORIES:
            continue
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
                value=category.score,
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
                    value=metric.value,
                )
            )
    if analyst_consensus is not None:
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
                value=analyst_consensus.rating.value,
            )
        )
        if analyst_consensus.upside_to_mean is not None:
            items.append(
                AIEvidenceItem(
                    evidence_id="analyst:upside_to_mean",
                    description=f"Upside to mean analyst target = {analyst_consensus.upside_to_mean:+.1%}",
                    source="Analyst Consensus",
                    value=analyst_consensus.upside_to_mean,
                )
            )
    if technical_summary is not None:
        items.append(
            AIEvidenceItem(
                evidence_id="technical:overall_rating",
                description=f"Technical overall rating = {technical_summary.overall_rating.value}",
                source="Technical Summary",
                value=technical_summary.overall_rating.value,
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


def _meets_minimum_evidence(
    dimensions: dict[str, AIDimensionAssessment], evidence_coverage: AIEvidenceCoverage
) -> bool:
    """The gate behind AI_MINIMUM_ASSESSABLE_DIMENSIONS/AI_MINIMUM_EVIDENCE_COVERAGE.

    Both conditions must hold: enough of the six dimensions were actually
    assessable, AND enough of the three evidence domains actually
    contributed. Either alone is gameable (six dimensions all thinly
    derived from one domain; or wide coverage but only one usable
    dimension) -- the combination is what "a meaningful synthesis" means
    here, per AI_RATING_METHODOLOGY_VERSION.
    """
    assessable = sum(1 for d in dimensions.values() if d.value != AIDimensionValue.REVIEW)
    return (
        assessable >= AI_MINIMUM_ASSESSABLE_DIMENSIONS
        and evidence_coverage.overall_ai_evidence_coverage >= AI_MINIMUM_EVIDENCE_COVERAGE
    )


def build_ai_research_assessment(
    *,
    ticker: str,
    raw: AIRawDimensions,
    evidence: list[AIEvidenceItem],
    evidence_coverage: AIEvidenceCoverage,
    research_schema_version: str,
    as_of: date,
    generated_at: datetime | None = None,
) -> AIResearchAssessment:
    """Validate provider evidence citations, then deterministically compute
    score/rating/confidence. Raises EvidenceViolation if the provider cited
    evidence it was never given -- never silently corrected.

    The overall rating is forced to REVIEW (and score to None) whenever
    evidence falls short of AI_MINIMUM_ASSESSABLE_DIMENSIONS /
    AI_MINIMUM_EVIDENCE_COVERAGE, even if the few available dimensions
    would otherwise average out to a confident-looking score -- see
    `_meets_minimum_evidence`. `confidence` is computed independently and
    is allowed to stay low-but-nonzero in that case: REVIEW + low
    confidence is the honest combination for sparse evidence, never a
    guessed Positive/Negative rating.
    """
    allowed_ids = {item.evidence_id for item in evidence}
    validate_evidence_ids(raw, allowed_ids)
    dimensions = raw.dimensions()
    confidence = compute_confidence(
        dimensions,
        evidence_count=len(evidence),
        evidence_coverage=evidence_coverage.overall_ai_evidence_coverage,
    )
    if _meets_minimum_evidence(dimensions, evidence_coverage):
        score = compute_score(dimensions)
        rating = _map_score_to_rating(score)
    else:
        score = None
        rating = AIDimensionValue.REVIEW
    supporting = sorted({eid for d in dimensions.values() for eid in d.supporting_evidence_ids})
    return AIResearchAssessment(
        ticker=ticker.upper(),
        score=score,
        rating=rating,
        confidence=confidence,
        dimensions=dimensions,
        evidence_coverage=evidence_coverage,
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


_RATING_WORD_TO_DIMENSION_VALUE: dict[str, AIDimensionValue] = {
    "STRONG_BUY": AIDimensionValue.VERY_POSITIVE,
    "BUY": AIDimensionValue.POSITIVE,
    "NEUTRAL": AIDimensionValue.NEUTRAL,
    "SELL": AIDimensionValue.NEGATIVE,
    "STRONG_SELL": AIDimensionValue.VERY_NEGATIVE,
    "REVIEW": AIDimensionValue.REVIEW,
}

# Analyst price-target upside, as a fraction (0.20 == +20%). Deliberately
# the same band shape as AnalystConsensus's own -2..+2 thresholds, just
# expressed on upside's native scale -- versioned alongside
# AI_RATING_METHODOLOGY_VERSION, not shared code, since the two domains'
# thresholds are allowed to diverge independently in the future.
_UPSIDE_THRESHOLDS_V1: tuple[tuple[float, AIDimensionValue], ...] = (
    (0.20, AIDimensionValue.VERY_POSITIVE),
    (0.05, AIDimensionValue.POSITIVE),
    (-0.05, AIDimensionValue.NEUTRAL),
    (-0.20, AIDimensionValue.NEGATIVE),
)
_UPSIDE_FLOOR = AIDimensionValue.VERY_NEGATIVE

# A blended dimension's average numeric signal maps back to a value using
# the same -2..+2 band shape as AnalystConsensus._RATING_THRESHOLDS_V1 --
# deliberately aligned so "moderately positive" means the same magnitude
# across AlphaLab's rating domains, though each still carries its own
# version and could diverge later without affecting the other.
_BLENDED_SIGNAL_THRESHOLDS_V1: tuple[tuple[float, AIDimensionValue], ...] = (
    (1.5, AIDimensionValue.VERY_POSITIVE),
    (0.5, AIDimensionValue.POSITIVE),
    (-0.5, AIDimensionValue.NEUTRAL),
    (-1.5, AIDimensionValue.NEGATIVE),
)
_BLENDED_SIGNAL_FLOOR = AIDimensionValue.VERY_NEGATIVE


def _band(value: float, thresholds: tuple[tuple[float, AIDimensionValue], ...], floor: AIDimensionValue) -> AIDimensionValue:
    for threshold, banded in thresholds:
        if value >= threshold:
            return banded
    return floor


def _dimension_value_for_evidence(evidence_id: str, value: float | str) -> AIDimensionValue | None:
    """Convert one evidence item's raw `value` to a dimension judgment.

    Returns None (not REVIEW) when the value itself doesn't represent a
    usable signal (e.g. an analyst/technical rating that is itself
    REVIEW) -- the caller treats that exactly like the source being
    absent, since averaging a "no signal" into a blend would be wrong.
    """
    if evidence_id.startswith("fundamental:"):
        return _map_score_to_rating(value if isinstance(value, (int, float)) else None)
    if evidence_id == "analyst:upside_to_mean" and isinstance(value, (int, float)):
        return _band(value, _UPSIDE_THRESHOLDS_V1, _UPSIDE_FLOOR)
    if evidence_id in ("analyst:rating", "technical:overall_rating") and isinstance(value, str):
        banded = _RATING_WORD_TO_DIMENSION_VALUE.get(value)
        return None if banded == AIDimensionValue.REVIEW else banded
    return None


class DeterministicAIRatingProvider(AIRatingProvider):
    """Rule-based, offline, no network. Each dimension is derived only from
    the evidence domains actually supplied for it -- see `_DIMENSION_SOURCES`
    -- and never claims to have used Analyst Consensus or Technical Summary
    evidence that was not present in the supplied payload.

    This is the default provider (see `configured_ai_rating_provider`) --
    unlike alpha_lab.ai.research's document-commentary AI (which is only
    ever active when explicitly configured with a live provider), a
    deterministic, fully-transparent mapping from evidence AlphaLab already
    trusts is always safe to show, never fabricates anything beyond what
    the evidence already says, and keeps this feature testable and usable
    with zero external dependency.

    Methodology: business/growth/competitive-position/risk stay strictly
    fundamental -- Technical Summary in particular is deliberately never
    blended into a business-quality-style dimension (a moving-average
    signal is not a fact about the business). Analyst Consensus feeds
    `business_outlook` (professional opinion is evidence about outlook) and
    `valuation_context` (price-target upside is a valuation signal).
    Technical Summary feeds only `catalyst_strength`, alongside the
    fundamental momentum category, as market/technical context. When two
    sources are blended and their signs disagree, a note is added to
    `contradictions` rather than silently averaging them away.
    """

    # dimension -> the evidence_ids it may be derived from, in the order
    # they are cited (not a priority order -- all present ones are blended
    # with equal weight).
    _DIMENSION_SOURCES: dict[str, tuple[str, ...]] = {
        "business_outlook": ("fundamental:business_quality", "analyst:rating"),
        "growth_prospects": ("fundamental:earnings_growth",),
        "competitive_position": ("fundamental:business_quality",),
        "valuation_context": ("fundamental:valuation", "analyst:upside_to_mean"),
        "risk_profile": ("fundamental:financial_strength",),
        "catalyst_strength": ("fundamental:momentum", "technical:overall_rating"),
    }

    def assess(self, ticker: str, evidence: list[AIEvidenceItem]) -> AIRawDimensions:
        by_id = {item.evidence_id: item for item in evidence}
        assessments: dict[str, AIDimensionAssessment] = {}
        contradictions: list[str] = []
        evidence_gaps: list[str] = []
        for dimension, source_ids in self._DIMENSION_SOURCES.items():
            assessment, contradiction = self._assess_dimension(dimension, source_ids, by_id)
            assessments[dimension] = assessment
            if contradiction:
                contradictions.append(contradiction)
            if assessment.value == AIDimensionValue.REVIEW:
                evidence_gaps.append(
                    f"No usable evidence for '{dimension}' among {', '.join(source_ids)}."
                )
        return AIRawDimensions(
            **assessments,
            positives=[],
            risks=[],
            catalysts=[],
            contradictions=contradictions,
            evidence_gaps=evidence_gaps,
            provider="deterministic-rule-based",
            model="category-threshold-v2",
            prompt_version=AI_RATING_PROMPT_VERSION,
        )

    def _assess_dimension(
        self, dimension: str, source_ids: tuple[str, ...], by_id: dict[str, AIEvidenceItem]
    ) -> tuple[AIDimensionAssessment, str | None]:
        used_ids: list[str] = []
        signals: list[float] = []
        reasoning_parts: list[str] = []
        for evidence_id in source_ids:
            item = by_id.get(evidence_id)
            if item is None or item.value is None:
                continue
            banded = _dimension_value_for_evidence(evidence_id, item.value)
            if banded is None:
                continue
            signals.append(_DIMENSION_NUMERIC[banded])
            used_ids.append(evidence_id)
            reasoning_parts.append(item.description)

        if not signals:
            return AIDimensionAssessment(value=AIDimensionValue.REVIEW, confidence=0.0), None

        contradiction = None
        if len(signals) > 1 and max(signals) > 0 and min(signals) < 0:
            contradiction = (
                f"{dimension}: evidence disagrees ({'; '.join(reasoning_parts)})."
            )
        average = sum(signals) / len(signals)
        value = _band(average, _BLENDED_SIGNAL_THRESHOLDS_V1, _BLENDED_SIGNAL_FLOOR)
        confidence = 0.6 if len(signals) == 1 else 0.75
        return (
            AIDimensionAssessment(
                value=value,
                confidence=confidence,
                reasoning="; ".join(reasoning_parts),
                supporting_evidence_ids=used_ids,
            ),
            contradiction,
        )


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
