"""Separate live, quantitative, AI, and historical evidence coverage."""

from pydantic import BaseModel, Field


class CoverageBreakdown(BaseModel):
    overall_live: float = Field(ge=0, le=1)
    quantitative: float = Field(ge=0, le=1)
    ai_research: float = Field(ge=0, le=1)
    historical: float = Field(ge=0, le=1)


def calculate_coverage(
    category_values: dict[str, float | None],
    weights: dict[str, float],
    *,
    ai_available: bool,
    historical_available_weight: float,
) -> CoverageBreakdown:
    quantitative_names = [name for name in weights if name != "ai_research"]
    quantitative_total = sum(weights[name] for name in quantitative_names)
    quantitative_available = sum(
        weights[name]
        for name in quantitative_names
        if category_values.get(name) is not None
    )
    quantitative = (
        quantitative_available / quantitative_total if quantitative_total else 0.0
    )
    ai_coverage = 1.0 if ai_available else 0.0
    overall = sum(
        weight
        for name, weight in weights.items()
        if category_values.get(name) is not None
    )
    return CoverageBreakdown(
        overall_live=min(1.0, overall),
        quantitative=quantitative,
        ai_research=ai_coverage,
        historical=max(0.0, min(1.0, historical_available_weight)),
    )
