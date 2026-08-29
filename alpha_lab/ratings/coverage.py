"""Separate live, quantitative, AI, and historical evidence coverage."""

import math

from pydantic import BaseModel, Field


class CoverageBreakdown(BaseModel):
    overall_live: float = Field(ge=0, le=1)
    quantitative: float = Field(ge=0, le=1)
    ai_research: float = Field(ge=0, le=1)
    historical: float = Field(ge=0, le=1)


def calculate_coverage(
    category_coverage: dict[str, float],
    weights: dict[str, float],
    *,
    ai_available: bool,
    historical_available_weight: float,
) -> CoverageBreakdown:
    """Weight metric-level evidence completeness; category score presence is irrelevant."""
    normalized = {
        name: _coverage_value(category_coverage.get(name, 0.0)) for name in weights
    }
    if not ai_available:
        normalized["ai_research"] = 0.0
    quantitative_names = [name for name in weights if name != "ai_research"]
    quantitative_total = sum(weights[name] for name in quantitative_names)
    quantitative_weighted = sum(
        weights[name] * normalized[name] for name in quantitative_names
    )
    overall = sum(weights[name] * normalized[name] for name in weights)
    return CoverageBreakdown(
        overall_live=min(1.0, overall),
        quantitative=(
            quantitative_weighted / quantitative_total if quantitative_total else 0.0
        ),
        ai_research=normalized.get("ai_research", 0.0),
        historical=max(0.0, min(1.0, historical_available_weight)),
    )


def _coverage_value(value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError(
            "Category evidence coverage must be finite and between 0 and 1"
        )
    return number
