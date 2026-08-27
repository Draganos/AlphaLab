"""Transparent, repeatable composite scoring with explicit missing-data policy."""

from datetime import date
import hashlib
import json
import math

from pydantic import BaseModel, Field


class CompositeResult(BaseModel):
    score: float | None = Field(None, ge=0, le=100)
    contributions: dict[str, float]
    unavailable: list[str]
    coverage: float = Field(ge=0, le=1)
    score_version: str
    config_hash: str
    evaluation_date: date


SCORE_VERSION = "phase1.5-v1"


def configuration_hash(weights: dict[str, float], version: str = SCORE_VERSION) -> str:
    """Hash canonical scoring configuration independent of dictionary order."""
    payload = json.dumps({"version": version, "weights": weights}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def composite_score(
    category_scores: dict[str, float | None], weights: dict[str, float], evaluation_date: date
) -> CompositeResult:
    if abs(sum(weights.values()) - 1) > 1e-8:
        raise ValueError("Weights must total 1.0")
    available = {name: value for name, value in category_scores.items()
                 if name in weights and value is not None and math.isfinite(float(value))}
    if any(not 0 <= float(value) <= 100 for value in available.values()):
        raise ValueError("Category scores must be between 0 and 100")
    coverage = sum(weights[name] for name in available)
    if not available:
        return CompositeResult(score=None, contributions={}, unavailable=list(weights), coverage=0,
                               score_version=SCORE_VERSION, config_hash=configuration_hash(weights),
                               evaluation_date=evaluation_date)
    # Renormalization avoids treating unavailable (rather than bad) data as zero.
    contributions = {name: round(float(value) * weights[name] / coverage, 2) for name, value in available.items()}
    return CompositeResult(score=round(sum(contributions.values()), 2), contributions=contributions,
                           unavailable=[name for name in weights if name not in available], coverage=coverage,
                           score_version=SCORE_VERSION, config_hash=configuration_hash(weights),
                           evaluation_date=evaluation_date)


def interpretation(score: float | None) -> str:
    if score is None:
        return "Unavailable"
    if score >= 85:
        return "Exceptional candidate"
    if score >= 75:
        return "Strong"
    if score >= 65:
        return "Positive"
    if score >= 50:
        return "Neutral"
    if score >= 40:
        return "Weak"
    return "Avoid/review"
