"""Natural-language interpretation followed by deterministic stored-data screening."""

import re
from typing import Literal

from pydantic import BaseModel, Field


class ScreenCriteria(BaseModel):
    ethical_status: list[str] = Field(default_factory=lambda: ["PASS"])
    countries: list[str] = Field(default_factory=list)
    exchanges: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    minimum_overall_score: float | None = Field(None, ge=0, le=100)
    minimum_growth_score: float | None = Field(None, ge=0, le=100)
    maximum_debt_to_ebitda: float | None = None
    minimum_market_cap: float | None = Field(None, ge=0)
    minimum_coverage: float | None = Field(None, ge=0, le=1)
    sort: Literal["overall_score_desc", "overall_score_asc"] = "overall_score_desc"
    unsupported: list[str] = Field(default_factory=list)


class ScreenRecord(BaseModel):
    ticker: str
    company_name: str | None = None
    country: str | None = None
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    themes: list[str] = Field(default_factory=list)
    ethical_status: str = "UNKNOWN"
    overall_score: float | None = None
    overall_rank: int | None = None
    growth_score: float | None = None
    debt_to_ebitda: float | None = None
    market_cap: float | None = None
    coverage: float = 0.0


_THEME_PHRASES = {
    "artificial intelligence": "artificial intelligence",
    "ai healthcare": "healthcare",
    "semiconductor": "semiconductors",
    "data centre": "data centres",
    "data center": "data centres",
    "cybersecurity": "cybersecurity",
    "robotics": "robotics",
    "cloud": "cloud",
    "payment infrastructure": "payments",
    "payment": "payments",
    "aviation": "aviation",
    "airline": "aviation",
    "renewable": "renewable energy",
    "healthcare": "healthcare",
    "biotech": "biotech",
    "logistics": "logistics",
}
_SECTORS = {
    "technology": "Technology",
    "healthcare": "Healthcare",
    "financial": "Financials",
    "industrial": "Industrials",
    "energy": "Energy",
}


def interpret_query(query: str) -> ScreenCriteria:
    """Interpret supported phrases only; unknown conditions are explicitly disclosed."""
    lowered = query.casefold()
    criteria = ScreenCriteria()
    criteria.sectors = sorted(
        {value for word, value in _SECTORS.items() if word in lowered}
    )
    criteria.themes = sorted(
        {value for phrase, value in _THEME_PHRASES.items() if phrase in lowered}
    )
    score = re.search(
        r"(?:score|rating)\s+(?:above|over|at least)\s+(\d+(?:\.\d+)?)", lowered
    )
    if score:
        criteria.minimum_overall_score = float(score.group(1))
    debt = re.search(
        r"debt(?:\s*/\s*ebitda| to ebitda)?\s+(?:below|under|max(?:imum)?)\s+(\d+(?:\.\d+)?)",
        lowered,
    )
    if debt:
        criteria.maximum_debt_to_ebitda = float(debt.group(1))
    if "strong growth" in lowered:
        criteria.minimum_growth_score = 70
    unsupported_terms = {
        "similar to": "similar-company matching",
        "insider buying": "insider transactions",
        "short interest": "short-interest history",
    }
    criteria.unsupported = [
        label for phrase, label in unsupported_terms.items() if phrase in lowered
    ]
    return criteria


def apply_screen(
    records: list[ScreenRecord], criteria: ScreenCriteria
) -> list[ScreenRecord]:
    """Filter only caller-supplied records; this function can never invent securities."""
    filtered = [
        record.model_copy(deep=True) for record in records if _matches(record, criteria)
    ]
    reverse = criteria.sort.endswith("_desc")
    filtered.sort(
        key=lambda item: (item.overall_score is not None, item.overall_score or -1),
        reverse=reverse,
    )
    for rank, record in enumerate(filtered, 1):
        record.overall_rank = rank
    return filtered


def _matches(record: ScreenRecord, criteria: ScreenCriteria) -> bool:
    if record.ethical_status not in criteria.ethical_status:
        return False
    if criteria.countries and record.country not in criteria.countries:
        return False
    if criteria.exchanges and record.exchange not in criteria.exchanges:
        return False
    if criteria.sectors and record.sector not in criteria.sectors:
        return False
    if criteria.industries and record.industry not in criteria.industries:
        return False
    if criteria.themes and not set(criteria.themes).issubset(record.themes):
        return False
    if criteria.minimum_overall_score is not None and (
        record.overall_score is None
        or record.overall_score < criteria.minimum_overall_score
    ):
        return False
    if criteria.minimum_growth_score is not None and (
        record.growth_score is None
        or record.growth_score < criteria.minimum_growth_score
    ):
        return False
    if criteria.maximum_debt_to_ebitda is not None and (
        record.debt_to_ebitda is None
        or record.debt_to_ebitda > criteria.maximum_debt_to_ebitda
    ):
        return False
    if criteria.minimum_market_cap is not None and (
        record.market_cap is None or record.market_cap < criteria.minimum_market_cap
    ):
        return False
    return (
        criteria.minimum_coverage is None
        or record.coverage >= criteria.minimum_coverage
    )
