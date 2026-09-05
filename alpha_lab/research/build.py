"""Deterministic conversion of a LiveResearchRecord into a StockResearch object.

No scores, percentiles, or coverage fractions are recomputed here — those are
owned by ``alpha_lab.ratings`` and ``alpha_lab.screener.service``. This module
only regroups already-computed evidence and derives two new, explicitly
documented values: a numeric ``confidence`` (0-10) and deterministic
strengths/weaknesses/risks bullets. See module docstring in
``alpha_lab.research.model`` for scale/scope decisions.
"""

from datetime import UTC, date, datetime

from alpha_lab.providers.capabilities import Capability, capability
from alpha_lab.research.formulas import (
    CAPABILITY_FIELDS_BY_METRIC,
    CAPPED_CAPABILITY_METRICS,
    DIRECTLY_SOURCED_METRICS,
    FORMULAS,
    KNOWN_INPUT_METRICS,
    metric_unit,
)
from alpha_lab.research.model import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    CategoryResult,
    CategoryStatus,
    MetricEvidence,
    MetricStatus,
    StockResearch,
)
from alpha_lab.screener.service import CATEGORY_EVIDENCE_METRICS, LiveResearchRecord

# A category is AVAILABLE only once every documented metric for it is
# present; anything less (but still scored) is PARTIAL. This mirrors the
# existing metric-level coverage semantics documented in the README
# ("Coverage and rating model").
_FULL_COVERAGE = 0.999

# Deterministic strength/weakness thresholds on the existing 0-100 category
# score scale. Chosen to sit strictly between the "Weak"/"Neutral"/"Positive"
# bands already defined in alpha_lab.strategy.scoring.interpretation
# (40/50/65/75/85) so a category is never simultaneously silent and extreme.
_STRENGTH_THRESHOLD = 70.0
_WEAKNESS_THRESHOLD = 30.0

# Capability -> confidence weight. Reuses alpha_lab.providers.capabilities'
# existing Capability enum rather than a second reliability system: a
# metric only gets full source-quality credit when the *specific field it
# was computed from* is documented reliable for that provider, not merely
# because the provider is reliable for something else (e.g. YFinance's
# current prices are RELIABLE_CURRENT but its fundamentals are PARTIAL).
_CAPABILITY_WEIGHTS = {
    Capability.RELIABLE_CURRENT: 1.0,
    Capability.RELIABLE_POINT_IN_TIME: 1.0,
    Capability.PARTIAL: 0.5,
    Capability.UNSUPPORTED: 0.0,
}

# Flat penalty applied to confidence when the live data-quality check
# (alpha_lab.screener.service._live_data_quality_reason) found an issue.
_DATA_QUALITY_PENALTY = 0.6

# Fundamentals/estimates are legitimately reported quarterly; treat evidence
# as fully fresh for 90 days and linearly decay to stale by one year. This
# is measured from each metric's own evidence `period` (fiscal period end,
# price date, or estimate observation date) — never from ingestion or
# metadata-refresh timestamps, which say only when AlphaLab last talked to
# a provider and can be recent even when the underlying evidence is not.
_FRESH_WINDOW_DAYS = 90
_STALE_WINDOW_DAYS = 365


def build_stock_research(
    record: LiveResearchRecord, *, generated_at: datetime | None = None
) -> StockResearch:
    categories = {
        name: _build_category(name, record) for name in CATEGORY_ORDER
    }
    strengths, weaknesses, risks = _strengths_weaknesses_risks(
        categories, record.data_quality_status
    )
    sources = sorted(
        {source for category in categories.values() for source in category.sources}
    )
    confidence = _confidence(record, categories)
    return StockResearch(
        ticker=record.ticker,
        company_name=record.company,
        sector=record.sector,
        industry=record.industry,
        security_type=record.asset_type,
        categories=categories,
        overall_score=record.overall_score,
        overall_coverage=record.overall_live_coverage,
        confidence=confidence,
        confidence_label=record.confidence,
        strengths=strengths,
        weaknesses=weaknesses,
        risks=risks,
        catalysts=[],
        sources=sources,
        data_quality_status=record.data_quality_status,
        evaluation_date=record.evaluation_date,
        generated_at=generated_at or datetime.now(UTC),
    )


def _build_category(name: str, record: LiveResearchRecord) -> CategoryResult:
    metric_names = CATEGORY_EVIDENCE_METRICS[name]
    metric_provenance = record.provenance.get("metrics", {})
    metrics: list[MetricEvidence] = []
    evidence: list[str] = []
    unavailable: list[str] = []
    for metric_name in metric_names:
        value = record.raw_metrics.get(metric_name)
        provenance = metric_provenance.get(metric_name) or {}
        available = value is not None
        percentile = record.percentile_metrics.get(metric_name) if available else None
        if not available:
            unavailable.append(metric_name)
        metrics.append(
            MetricEvidence(
                name=metric_name,
                value=value,
                percentile=percentile,
                unit=metric_unit(metric_name),
                period=provenance.get("period")
                or provenance.get("publication_date")
                or provenance.get("observation_date"),
                source=provenance.get("provider"),
                retrieved_at=provenance.get("ingested_at"),
                is_calculated=metric_name not in DIRECTLY_SOURCED_METRICS,
                formula=FORMULAS.get(metric_name),
                inputs=_known_inputs(metric_name, record),
                status=MetricStatus.AVAILABLE if available else MetricStatus.UNAVAILABLE,
            )
        )
        if available:
            detail = f"{metric_name} = {value:.4g}"
            if percentile is not None:
                detail += f" (percentile {percentile:.0f})"
            evidence.append(detail)
    if name == "ai_research" and record.category_scores.get(name) is not None:
        evidence.append(f"ai_research rating = {record.category_scores[name]:.4g}")

    score = record.category_scores.get(name)
    coverage = record.category_coverage.get(name, 0.0)
    if score is None:
        status = CategoryStatus.UNAVAILABLE
    elif coverage >= _FULL_COVERAGE:
        status = CategoryStatus.AVAILABLE
    else:
        status = CategoryStatus.PARTIAL
    sources = sorted({item.source for item in metrics if item.source})
    ai_provenance = record.provenance.get("ai") or {}
    if name == "ai_research" and ai_provenance.get("provider"):
        sources = sorted({*sources, ai_provenance["provider"]})
    return CategoryResult(
        name=name,
        label=CATEGORY_LABELS[name],
        score=score,
        coverage=coverage,
        status=status,
        metrics=metrics,
        evidence=evidence,
        unavailable_metrics=unavailable,
        sources=sources,
    )


def _known_inputs(
    metric_name: str, record: LiveResearchRecord
) -> dict[str, float | int | None] | None:
    input_names = KNOWN_INPUT_METRICS.get(metric_name)
    if not input_names:
        return None
    return {name: record.raw_metrics.get(name) for name in input_names}


def _strengths_weaknesses_risks(
    categories: dict[str, CategoryResult], data_quality_status: str
) -> tuple[list[str], list[str], list[str]]:
    strengths: list[str] = []
    weaknesses: list[str] = []
    risks: list[str] = []
    for name in CATEGORY_ORDER:
        category = categories[name]
        if category.status == CategoryStatus.UNAVAILABLE:
            risks.append(
                f"{category.label} is unavailable — excluded from the assessment, "
                "not treated as a negative signal."
            )
            continue
        if category.score is None:
            continue
        if category.score >= _STRENGTH_THRESHOLD:
            strengths.append(
                f"{category.label} scores {category.score:.0f}/100 "
                f"(coverage {category.coverage:.0%})."
            )
        elif category.score <= _WEAKNESS_THRESHOLD:
            weaknesses.append(
                f"{category.label} scores {category.score:.0f}/100 "
                f"(coverage {category.coverage:.0%})."
            )
    if data_quality_status != "valid":
        risks.append(f"Price data quality issue detected: {data_quality_status}.")
    return strengths, weaknesses, risks


def _confidence(record: LiveResearchRecord, categories: dict[str, CategoryResult]) -> float:
    """Deterministic 0-10 confidence. See module docstring for the formula.

    confidence = 10 * data_quality_penalty * clip01(
        0.5 * overall_coverage
        + 0.2 * category_breadth
        + 0.2 * freshness_factor
        + 0.1 * source_quality_factor
    )

    category_breadth is the mean of the eight categories' own (already
    computed) `coverage` fractions — not whether each category cleared its
    minimum-metric threshold for a score — so a category that has real but
    insufficient evidence for a score still counts partially rather than as
    "no evidence".
    """
    overall_coverage = _clip01(record.overall_live_coverage)
    category_breadth = sum(
        category.coverage for category in categories.values()
    ) / len(CATEGORY_ORDER)
    freshness_factor = _freshness_factor(record.evaluation_date, categories)
    source_quality_factor = _source_quality_factor(categories)
    penalty = 1.0 if record.data_quality_status == "valid" else _DATA_QUALITY_PENALTY
    raw = (
        0.5 * overall_coverage
        + 0.2 * category_breadth
        + 0.2 * freshness_factor
        + 0.1 * source_quality_factor
    )
    return round(10 * penalty * _clip01(raw), 1)


def _freshness_factor(evaluation_date: date, categories: dict[str, CategoryResult]) -> float:
    periods = [
        metric.period
        for category in categories.values()
        for metric in category.metrics
        if metric.status == MetricStatus.AVAILABLE and metric.period is not None
    ]
    if not periods:
        return 0.0
    age_days = (evaluation_date - min(periods)).days
    if age_days <= _FRESH_WINDOW_DAYS:
        return 1.0
    decay_span = _STALE_WINDOW_DAYS - _FRESH_WINDOW_DAYS
    return _clip01(1 - (age_days - _FRESH_WINDOW_DAYS) / decay_span)


def _source_quality_factor(categories: dict[str, CategoryResult]) -> float:
    weights = [
        _metric_capability_weight(metric.name, metric.source)
        for category in categories.values()
        for metric in category.metrics
        if metric.status == MetricStatus.AVAILABLE
    ]
    return sum(weights) / len(weights) if weights else 0.0


def _metric_capability_weight(metric_name: str, provider: str | None) -> float:
    if not provider:
        return 0.0
    if metric_name in CAPPED_CAPABILITY_METRICS:
        return CAPPED_CAPABILITY_METRICS[metric_name]
    field = CAPABILITY_FIELDS_BY_METRIC.get(metric_name, {}).get(provider)
    if field is None:
        return 0.0
    return _CAPABILITY_WEIGHTS[capability(provider, field)]


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))
