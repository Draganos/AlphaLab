"""Transparent, repeatable composite scoring with explicit missing-data policy."""

from datetime import date
import hashlib
import json
import math

from pydantic import BaseModel, Field

from alpha_lab.config import CoverageSettings


class CompositeResult(BaseModel):
    score: float | None = Field(None, ge=0, le=100)
    contributions: dict[str, float]
    unavailable: list[str]
    coverage: float = Field(ge=0, le=1)
    score_version: str
    config_hash: str
    evaluation_date: date
    raw_interpretation: str
    confidence_label: str


SCORE_VERSION = "phase1.5-v1"


def configuration_hash(
    weights: dict[str, float], coverage: CoverageSettings | None = None, version: str = SCORE_VERSION
) -> str:
    """Hash canonical scoring configuration independent of dictionary order."""
    payload = json.dumps({"version": version, "weights": weights,
                          "coverage": coverage.model_dump() if coverage else None},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def composite_score(
    category_scores: dict[str, float | None], weights: dict[str, float], evaluation_date: date,
    coverage_settings: CoverageSettings,
) -> CompositeResult:
    if abs(sum(weights.values()) - 1) > 1e-8:
        raise ValueError("Weights must total 1.0")
    available = {name: value for name, value in category_scores.items()
                 if name in weights and value is not None and math.isfinite(float(value))}
    if any(not 0 <= float(value) <= 100 for value in available.values()):
        raise ValueError("Category scores must be between 0 and 100")
    coverage = sum(weights[name] for name in available)
    if not available:
        label = coverage_interpretation(None, 0, coverage_settings)
        return CompositeResult(score=None, contributions={}, unavailable=list(weights), coverage=0,
                               score_version=SCORE_VERSION, config_hash=configuration_hash(weights, coverage_settings),
                               evaluation_date=evaluation_date, raw_interpretation="Unavailable",
                               confidence_label=label)
    # Renormalization avoids treating unavailable (rather than bad) data as zero.
    contributions = {name: round(float(value) * weights[name] / coverage, 2) for name, value in available.items()}
    score = round(sum(contributions.values()), 2)
    raw_label = interpretation(score)
    return CompositeResult(score=score, contributions=contributions,
                           unavailable=[name for name in weights if name not in available], coverage=coverage,
                           score_version=SCORE_VERSION, config_hash=configuration_hash(weights, coverage_settings),
                           evaluation_date=evaluation_date, raw_interpretation=raw_label,
                           confidence_label=coverage_interpretation(score, coverage, coverage_settings))


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


def coverage_interpretation(score: float | None, coverage: float, settings: CoverageSettings) -> str:
    """Qualify the raw label so low-coverage scores never imply normal confidence."""
    if coverage < settings.insufficient_below:
        return "Insufficient data"
    raw = interpretation(score)
    if coverage < settings.full_confidence_at:
        return f"Provisional — {raw}"
    return raw
