"""Pure, deterministic comparison between two StockResearch snapshots.

This is historical *comparison*, not causal explanation: it reports what
changed (a value, a status, evidence appearing/disappearing) without
inferring why. It never fabricates a narrative like "valuation improved
because the price fell" — only the underlying evidence, unchanged, is ever
shown alongside the diff.

Takes two already-loaded StockResearch objects; it does not know about
persistence, snapshot IDs, or the database. alpha_lab.research.service
wires this to ResearchSnapshotRepository for callers that only have
snapshot IDs.
"""

from datetime import date, datetime
from enum import StrEnum
import math

from pydantic import BaseModel

from alpha_lab.research.model import (
    CATEGORY_ORDER,
    CategoryStatus,
    MetricStatus,
    StockResearch,
)


class ChangeType(StrEnum):
    MISSING_TO_VALUE = "MISSING_TO_VALUE"
    VALUE_TO_MISSING = "VALUE_TO_MISSING"
    VALUE_CHANGED = "VALUE_CHANGED"
    STATUS_CHANGED = "STATUS_CHANGED"
    NO_CHANGE = "NO_CHANGE"


class MetricChange(BaseModel):
    metric: str
    change_type: ChangeType
    old_value: float | int | None
    new_value: float | int | None
    old_status: MetricStatus
    new_status: MetricStatus


class CategoryComparison(BaseModel):
    category: str
    label: str
    old_score: float | None
    new_score: float | None
    score_changed: bool
    old_coverage: float
    new_coverage: float
    coverage_changed: bool
    old_status: CategoryStatus
    new_status: CategoryStatus
    status_changed: bool
    # Only metrics whose change_type != NO_CHANGE — this is a diff, not a
    # full re-listing of every metric in the category.
    metric_changes: list[MetricChange]


class ResearchComparison(BaseModel):
    ticker: str
    older_evaluation_date: date
    newer_evaluation_date: date
    older_generated_at: datetime
    newer_generated_at: datetime
    rating_version_changed: bool
    configuration_changed: bool
    overall_score_old: float | None
    overall_score_new: float | None
    overall_score_changed: bool
    overall_coverage_old: float
    overall_coverage_new: float
    overall_coverage_changed: bool
    confidence_old: float
    confidence_new: float
    confidence_changed: bool
    categories: list[CategoryComparison]


def compare_stock_research(older: StockResearch, newer: StockResearch) -> ResearchComparison:
    """Diff two StockResearch objects. Caller decides which is older/newer;
    this does not assume evaluation_date ordering (a restatement can make a
    newly-generated snapshot describe an earlier evaluation_date)."""
    if older.ticker != newer.ticker:
        raise ValueError(
            f"Cannot compare research for different tickers: {older.ticker} vs {newer.ticker}"
        )
    categories = [
        _compare_category(older.categories[name], newer.categories[name])
        for name in CATEGORY_ORDER
    ]
    return ResearchComparison(
        ticker=older.ticker,
        older_evaluation_date=older.evaluation_date,
        newer_evaluation_date=newer.evaluation_date,
        older_generated_at=older.generated_at,
        newer_generated_at=newer.generated_at,
        rating_version_changed=older.rating_version != newer.rating_version,
        configuration_changed=older.configuration_hash != newer.configuration_hash,
        overall_score_old=older.overall_score,
        overall_score_new=newer.overall_score,
        overall_score_changed=not _values_equal(older.overall_score, newer.overall_score),
        overall_coverage_old=older.overall_coverage,
        overall_coverage_new=newer.overall_coverage,
        overall_coverage_changed=not _values_equal(older.overall_coverage, newer.overall_coverage),
        confidence_old=older.confidence,
        confidence_new=newer.confidence,
        confidence_changed=not _values_equal(older.confidence, newer.confidence),
        categories=categories,
    )


def _compare_category(older, newer) -> CategoryComparison:
    older_metrics = {metric.name: metric for metric in older.metrics}
    newer_metrics = {metric.name: metric for metric in newer.metrics}
    metric_changes = []
    for name in sorted(set(older_metrics) & set(newer_metrics)):
        change = _compare_metric(name, older_metrics[name], newer_metrics[name])
        if change.change_type != ChangeType.NO_CHANGE:
            metric_changes.append(change)
    return CategoryComparison(
        category=older.name,
        label=older.label,
        old_score=older.score,
        new_score=newer.score,
        score_changed=not _values_equal(older.score, newer.score),
        old_coverage=older.coverage,
        new_coverage=newer.coverage,
        coverage_changed=not _values_equal(older.coverage, newer.coverage),
        old_status=older.status,
        new_status=newer.status,
        status_changed=older.status != newer.status,
        metric_changes=metric_changes,
    )


def _compare_metric(name: str, older, newer) -> MetricChange:
    old_value, new_value = older.value, newer.value
    if old_value is None and new_value is not None:
        change_type = ChangeType.MISSING_TO_VALUE
    elif old_value is not None and new_value is None:
        change_type = ChangeType.VALUE_TO_MISSING
    elif old_value is not None and new_value is not None and not _values_equal(old_value, new_value):
        change_type = ChangeType.VALUE_CHANGED
    elif older.status != newer.status:
        change_type = ChangeType.STATUS_CHANGED
    else:
        change_type = ChangeType.NO_CHANGE
    return MetricChange(
        metric=name,
        change_type=change_type,
        old_value=old_value,
        new_value=new_value,
        old_status=older.status,
        new_status=newer.status,
    )


def _values_equal(old: float | int | None, new: float | int | None) -> bool:
    if old is None or new is None:
        return old is new
    return math.isclose(float(old), float(new), rel_tol=1e-9, abs_tol=1e-9)
