"""Canonical, evidence-first research object.

This module defines a presentation/audit layer on top of
:class:`alpha_lab.screener.service.LiveResearchRecord`. It does not compute
any new scores, percentiles, or coverage numbers — those remain the single
responsibility of ``alpha_lab.ratings``/``alpha_lab.screener``. Instead it
regroups the existing flat ``category_scores`` / ``category_coverage`` /
``raw_metrics`` / ``percentile_metrics`` / ``provenance`` dictionaries into a
single auditable :class:`StockResearch` object with explicit per-metric
availability and a deterministic, documented confidence score.

Design notes / deliberate scope limits:

* ``MetricStatus.NOT_APPLICABLE`` exists so security-type-aware applicability
  (banks, REITs, ETFs, ...) has somewhere to plug in later; this module does
  not yet classify any metric as not applicable — see AlphaLab project brief
  section 13. Every metric considered here defaults to AVAILABLE/UNAVAILABLE.
* ``MetricStatus.INVALID`` is reserved and currently unused. Verified against
  the live pipeline: ``alpha_lab.data_quality.assess_field``/``assess_freshness``
  (the module's only INVALID-adjacent machinery, using ``QualityStatus``) is
  wired only into the legacy ``app/dashboard/main.py`` page, not into
  ``alpha_lab.screener.service`` / ``alpha_lab.ratings``. There, defensive
  numeric coercion (``_number``/``_positive``/``_finite`` helpers in
  ``alpha_lab.ratings.quality`` and ``alpha_lab.ratings.valuation``) already
  keeps non-finite or wrong-sign inputs out of a computed ratio, but does so
  by silently returning ``None`` — indistinguishable from a value that was
  simply never reported. There is today no guarantee, and no signal, that a
  present-but-invalid input was ever distinguished from a genuinely missing
  one before reaching ``LiveResearchRecord.raw_metrics``. Wiring
  ``MetricStatus.INVALID`` up for real would mean changing those coercion
  helpers to return a reason alongside ``None`` (e.g. "negative denominator"
  vs "no observation") — a change to the rating engine itself, out of scope
  here; this module only reserves the state for that future integration
  point.
* Category and overall *scores* stay on the existing 0-100 scale used
  throughout ``alpha_lab.ratings``/``alpha_lab.screener`` and the Streamlit
  UI, to avoid introducing a second, easily-desynchronized scale. Confidence
  is a new value and uses a 0-10 scale, matching the project brief's
  presentation example, since no prior convention exists for it.
"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

CATEGORY_LABELS: dict[str, str] = {
    "business_quality": "Business Quality",
    "earnings_growth": "Earnings Growth",
    "financial_strength": "Financial Strength",
    "momentum": "Momentum",
    "valuation": "Valuation",
    "shareholder_return": "Shareholder Return",
    "analyst_revisions": "Analyst Revisions",
    "ai_research": "AI Research",
}

# Canonical display order per the AlphaLab project brief; do not reorder,
# rename, merge, or drop categories without an explicit product decision.
CATEGORY_ORDER: tuple[str, ...] = (
    "business_quality",
    "earnings_growth",
    "financial_strength",
    "momentum",
    "valuation",
    "shareholder_return",
    "analyst_revisions",
    "ai_research",
)


class MetricStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INVALID = "INVALID"


class CategoryStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class MetricEvidence(BaseModel):
    name: str
    value: float | int | None
    # Unrounded percentile (0-100, relative to the eligible universe) from
    # alpha_lab.screener.service — the exact input the rating engine used to
    # compute the category score. `evidence` strings may round this for
    # display; this field never does, so a category score can be reproduced
    # from this object alone.
    percentile: float | None = None
    unit: str | None
    period: date | None = None
    source: str | None = None
    retrieved_at: datetime | None = None
    is_calculated: bool
    formula: str | None = None
    inputs: dict[str, float | int | None] | None = None
    status: MetricStatus


class CategoryResult(BaseModel):
    name: str
    label: str
    score: float | None = Field(None, ge=0, le=100)
    coverage: float = Field(ge=0, le=1)
    status: CategoryStatus
    metrics: list[MetricEvidence]
    evidence: list[str]
    unavailable_metrics: list[str]
    sources: list[str]


class StockResearch(BaseModel):
    ticker: str
    company_name: str | None
    sector: str | None
    industry: str | None
    security_type: str | None
    categories: dict[str, CategoryResult]
    overall_score: float | None = Field(None, ge=0, le=100)
    overall_coverage: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=10)
    # Deterministic band derived from `confidence` itself (see
    # alpha_lab.research.build._confidence_label) — kept distinct from
    # `score_interpretation` below, which is a *different* legacy label
    # (alpha_lab.strategy.scoring.coverage_interpretation) driven only by
    # overall_score/overall_coverage and blind to freshness, source
    # quality, or a data-quality penalty. A record can score well and be
    # fully covered yet still carry low `confidence` because its evidence
    # is stale or partial-quality; conflating the two labels would let a
    # high score_interpretation mask that.
    confidence_label: str
    score_interpretation: str
    strengths: list[str]
    weaknesses: list[str]
    risks: list[str]
    catalysts: list[str]
    sources: list[str]
    data_quality_status: str
    # Carried from LiveResearchRecord so a persisted/compared StockResearch
    # can be traced back to the scoring configuration that produced its
    # scores — the same evidence under a different rating_weights config
    # can score differently, and this is the only place that's recorded.
    rating_version: str
    configuration_hash: str
    evaluation_date: date
    generated_at: datetime
