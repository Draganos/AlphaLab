"""Transparent configurable composite scoring with explicit missing-data policy."""

from pydantic import BaseModel, Field


class CompositeResult(BaseModel):
    score: float | None = Field(None, ge=0, le=100)
    contributions: dict[str, float]
    unavailable: list[str]
    coverage: float = Field(ge=0, le=1)


def composite_score(category_scores: dict[str, float | None], weights: dict[str, float]) -> CompositeResult:
    if abs(sum(weights.values()) - 1) > 1e-8:
        raise ValueError("Weights must total 1.0")
    available = {name: value for name, value in category_scores.items() if name in weights and value is not None}
    coverage = sum(weights[name] for name in available)
    if not available:
        return CompositeResult(score=None, contributions={}, unavailable=list(weights), coverage=0)
    # Renormalization avoids treating unavailable (rather than bad) data as zero.
    contributions = {name: round(float(value) * weights[name] / coverage, 2) for name, value in available.items()}
    return CompositeResult(score=round(sum(contributions.values()), 2), contributions=contributions,
                           unavailable=[name for name in weights if name not in available], coverage=coverage)


def interpretation(score: float | None) -> str:
    if score is None: return "Unavailable"
    if score >= 85: return "Exceptional candidate"
    if score >= 75: return "Strong"
    if score >= 65: return "Positive"
    if score >= 50: return "Neutral"
    if score >= 40: return "Weak"
    return "Avoid/review"
