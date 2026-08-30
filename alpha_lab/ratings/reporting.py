"""Coverage acceptance reporting over persisted current research records."""

from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class CoverageReport:
    count: int
    median: float | None
    p25: float | None
    p75: float | None
    minimum: float | None
    maximum: float | None
    category_availability: dict[str, float]
    unavailable_reasons: dict[str, int]


def summarize_coverage(records: list) -> CoverageReport:
    """Aggregate genuine metric-level coverage; no missing value is imputed."""
    if not records:
        return CoverageReport(0, None, None, None, None, None, {}, {})
    values = np.array([record.overall_live_coverage for record in records], dtype=float)
    categories = sorted({key for record in records for key in record.category_coverage})
    category_availability = {
        category: float(np.mean([record.category_coverage.get(category, 0.0) for record in records]))
        for category in categories
    }
    unavailable: dict[str, int] = {}
    for record in records:
        for category, coverage in record.category_coverage.items():
            if coverage == 0:
                unavailable[category] = unavailable.get(category, 0) + 1
    return CoverageReport(
        count=len(records), median=float(np.median(values)),
        p25=float(np.percentile(values, 25)), p75=float(np.percentile(values, 75)),
        minimum=float(values.min()), maximum=float(values.max()),
        category_availability=category_availability,
        unavailable_reasons=unavailable,
    )
