"""Validated query interpretation followed by deterministic stored-data screening."""

from abc import ABC, abstractmethod
import json
import math
import os
import re
from typing import Literal
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field


class ScreenCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text_search: str | None = None
    ethical_status: list[str] = Field(default_factory=lambda: ["PASS"])
    countries: list[str] = Field(default_factory=list)
    exchanges: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    minimum_overall_score: float | None = Field(None, ge=0, le=100)
    minimum_coverage: float | None = Field(None, ge=0, le=1)
    minimum_growth_score: float | None = Field(None, ge=0, le=100)
    minimum_revisions_score: float | None = Field(None, ge=0, le=100)
    minimum_quality_score: float | None = Field(None, ge=0, le=100)
    minimum_valuation_score: float | None = Field(None, ge=0, le=100)
    minimum_momentum_score: float | None = Field(None, ge=0, le=100)
    minimum_financial_strength_score: float | None = Field(None, ge=0, le=100)
    minimum_ai_research_score: float | None = Field(None, ge=0, le=100)
    minimum_shareholder_return_score: float | None = Field(None, ge=0, le=100)
    maximum_debt_to_ebitda: float | None = Field(None, ge=0)
    minimum_market_cap: float | None = Field(None, ge=0)
    maximum_market_cap: float | None = Field(None, ge=0)
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
    overall_score: float | None = Field(None, ge=0, le=100)
    overall_rank: int | None = None
    growth_score: float | None = Field(None, ge=0, le=100)
    revisions_score: float | None = Field(None, ge=0, le=100)
    quality_score: float | None = Field(None, ge=0, le=100)
    valuation_score: float | None = Field(None, ge=0, le=100)
    momentum_score: float | None = Field(None, ge=0, le=100)
    financial_strength_score: float | None = Field(None, ge=0, le=100)
    ai_research_score: float | None = Field(None, ge=0, le=100)
    shareholder_return_score: float | None = Field(None, ge=0, le=100)
    debt_to_ebitda: float | None = Field(None, ge=0)
    market_cap: float | None = Field(None, ge=0)
    coverage: float = Field(0.0, ge=0, le=1)


class QueryInterpretationProvider(ABC):
    """Providers return filters only; securities always come from deterministic storage."""

    @abstractmethod
    def interpret(self, query: str) -> ScreenCriteria: ...


class DeterministicQueryInterpreter(QueryInterpretationProvider):
    """Offline conservative phrase interpreter used by default and in tests."""

    def interpret(self, query: str) -> ScreenCriteria:
        lowered = query.casefold().strip()
        criteria = ScreenCriteria()
        criteria.sectors = sorted(
            {
                value
                for word, value in _SECTORS.items()
                if re.search(rf"\b{re.escape(word)}\b", lowered)
            }
        )
        criteria.industries = sorted(
            {value for word, value in _INDUSTRIES.items() if word in lowered}
        )
        criteria.themes = sorted(
            {value for phrase, value in _THEMES.items() if phrase in lowered}
        )
        criteria.exchanges = sorted(
            exchange
            for exchange in ("NYSE", "NASDAQ")
            if exchange.casefold() in lowered
        )
        if any(
            phrase in lowered
            for phrase in ("us stocks", "u.s. stocks", "united states stocks")
        ):
            criteria.countries = ["US"]
        if "sharia-preferred" in lowered or "sharia preferred" in lowered:
            criteria.ethical_status = ["PASS"]
        _score_threshold(lowered, criteria)
        _market_cap(lowered, criteria)
        mapped: set[str] = set()
        for phrase, field, value in _CONCEPTS:
            if phrase in lowered:
                current = getattr(criteria, field)
                setattr(criteria, field, max(current or 0, value))
                mapped.add(phrase)
        debt = re.search(
            r"debt(?:\s*/\s*ebitda| to ebitda)?\s+(?:below|under|max(?:imum)?)\s+(\d+(?:\.\d+)?)",
            lowered,
        )
        if debt:
            criteria.maximum_debt_to_ebitda = float(debt.group(1))
        unsupported_terms = {
            "similar to": "similar-company matching",
            "insider buying": "insider transactions",
            "short interest": "short-interest history",
            "autonomous driving": "autonomous-driving exposure",
        }
        criteria.unsupported.extend(
            label for phrase, label in unsupported_terms.items() if phrase in lowered
        )
        criteria.unsupported.extend(_unsupported_comparisons(query))
        return ScreenCriteria.model_validate(criteria.model_dump())


class OpenAIQueryInterpreter(QueryInterpretationProvider):
    """Optional credentialed interpreter constrained to the ScreenCriteria schema."""

    def __init__(self, api_key: str, model: str = "gpt-4.1-mini"):
        self.api_key, self.model = api_key, model

    def interpret(self, query: str) -> ScreenCriteria:
        schema = ScreenCriteria.model_json_schema()
        prompt = (
            "Translate the user request into the supplied AlphaLab filter schema. "
            "Never return ticker symbols or companies. Put every condition that cannot be represented "
            "in unsupported. Return JSON only.\nSchema: "
            + json.dumps(schema)
            + "\nQuery: "
            + query
        )
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }
        ).encode()
        request = Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=45) as response:  # noqa: S310 - fixed HTTPS endpoint
            body = json.loads(response.read())
        return ScreenCriteria.model_validate_json(
            body["choices"][0]["message"]["content"]
        )


def configured_query_interpreter() -> QueryInterpretationProvider:
    provider = os.getenv("ALPHALAB_QUERY_PROVIDER", "deterministic").casefold()
    if provider == "openai" and os.getenv("OPENAI_API_KEY"):
        return OpenAIQueryInterpreter(
            os.environ["OPENAI_API_KEY"],
            os.getenv("ALPHALAB_QUERY_MODEL", "gpt-4.1-mini"),
        )
    return DeterministicQueryInterpreter()


def interpret_query(query: str) -> ScreenCriteria:
    """Compatibility wrapper using the configured provider, failing closed offline."""
    try:
        return configured_query_interpreter().interpret(query)
    except Exception:
        criteria = DeterministicQueryInterpreter().interpret(query)
        criteria.unsupported.append("configured AI query interpretation unavailable")
        return criteria


def apply_screen(
    records: list[ScreenRecord], criteria: ScreenCriteria
) -> list[ScreenRecord]:
    """Filter only caller-supplied records; this function can never invent securities."""
    filtered = [
        record.model_copy(deep=True) for record in records if _matches(record, criteria)
    ]
    descending = criteria.sort.endswith("_desc")
    filtered.sort(
        key=lambda item: (
            not _usable_score(item.overall_score),
            (-item.overall_score if descending else item.overall_score)
            if _usable_score(item.overall_score)
            else 0,
            item.ticker,
        )
    )
    for rank, record in enumerate(filtered, 1):
        record.overall_rank = rank
    return filtered


def _usable_score(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and 0 <= value <= 100


def _matches(record: ScreenRecord, criteria: ScreenCriteria) -> bool:
    text = " ".join(
        value or ""
        for value in (
            record.ticker,
            record.company_name,
            record.sector,
            record.industry,
        )
    ).casefold()
    if criteria.text_search and criteria.text_search.casefold() not in text:
        return False
    if record.ethical_status not in criteria.ethical_status:
        return False
    for selected, actual in (
        (criteria.countries, record.country),
        (criteria.exchanges, record.exchange),
        (criteria.sectors, record.sector),
        (criteria.industries, record.industry),
    ):
        if selected and actual not in selected:
            return False
    if criteria.themes and not set(criteria.themes).issubset(record.themes):
        return False
    minimums = (
        (criteria.minimum_overall_score, record.overall_score),
        (criteria.minimum_growth_score, record.growth_score),
        (criteria.minimum_revisions_score, record.revisions_score),
        (criteria.minimum_quality_score, record.quality_score),
        (criteria.minimum_valuation_score, record.valuation_score),
        (criteria.minimum_momentum_score, record.momentum_score),
        (criteria.minimum_financial_strength_score, record.financial_strength_score),
        (criteria.minimum_ai_research_score, record.ai_research_score),
        (criteria.minimum_shareholder_return_score, record.shareholder_return_score),
        (criteria.minimum_market_cap, record.market_cap),
        (criteria.minimum_coverage, record.coverage),
    )
    if any(
        threshold is not None and (actual is None or actual < threshold)
        for threshold, actual in minimums
    ):
        return False
    if criteria.maximum_market_cap is not None and (
        record.market_cap is None or record.market_cap > criteria.maximum_market_cap
    ):
        return False
    return criteria.maximum_debt_to_ebitda is None or (
        record.debt_to_ebitda is not None
        and record.debt_to_ebitda <= criteria.maximum_debt_to_ebitda
    )


def _score_threshold(text: str, criteria: ScreenCriteria) -> None:
    score = re.search(
        r"(?:score|rating)\s+(?:above|over|at least)\s+([-+]?\d+(?:\.\d+)?)", text
    )
    if score:
        value = float(score.group(1))
        if 0 <= value <= 100:
            criteria.minimum_overall_score = value
        else:
            criteria.unsupported.append(score.group(0))
    coverage = re.search(
        r"coverage\s+(?:above|over|at least)\s+([-+]?\d+(?:\.\d+)?)(%)?", text
    )
    if coverage:
        value = float(coverage.group(1))
        normalized = value / 100 if coverage.group(2) or value > 1 else value
        if 0 <= normalized <= 1:
            criteria.minimum_coverage = normalized
        else:
            criteria.unsupported.append(coverage.group(0))


def _market_cap(text: str, criteria: ScreenCriteria) -> None:
    match = re.search(
        r"market cap\s+(?:above|over|at least)\s+\$?(\d+(?:\.\d+)?)\s*([bmk])?", text
    )
    if match:
        multiplier = {"b": 1e9, "m": 1e6, "k": 1e3}.get(match.group(2), 1)
        criteria.minimum_market_cap = float(match.group(1)) * multiplier


def _unsupported_comparisons(query: str) -> list[str]:
    """Disclose raw-metric comparisons that ScreenCriteria cannot execute yet."""
    metric = (
        r"P\s*/\s*E|forward\s+P\s*/\s*E|EV\s*/\s*EBITDA|price\s*/\s*sales|"
        r"price\s*/\s*FCF|FCF\s+yield|earnings\s+yield|dividend\s+yield|"
        r"ROE|ROA|ROIC|revenue\s+growth|EPS\s+growth|net\s+margin|gross\s+margin"
    )
    comparison = (
        r"(?:below|under|above|over|at\s+least|at\s+most|less\s+than|greater\s+than)"
    )
    pattern = re.compile(
        rf"\b(?:{metric})\s+{comparison}\s+[-+]?\$?\d+(?:\.\d+)?%?",
        re.IGNORECASE,
    )
    return [match.group(0).strip() for match in pattern.finditer(query)]


_THEMES = {
    "artificial intelligence": "artificial intelligence",
    "ai healthcare": "healthcare",
    "semiconductor": "semiconductors",
    "data centre": "data centres",
    "data center": "data centres",
    "data-centre": "data centres",
    "data-center": "data centres",
    "cybersecurity": "cybersecurity",
    "robotics": "robotics",
    "cloud": "cloud",
    "payment infrastructure": "payments",
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
_INDUSTRIES = {
    "airline industry": "Airlines",
    "semiconductor industry": "Semiconductors",
    "banking industry": "Banks",
}
_CONCEPTS = (
    ("strong growth", "minimum_growth_score", 70),
    ("improving fundamentals", "minimum_growth_score", 60),
    ("positive revisions", "minimum_revisions_score", 55),
    ("positive earnings revisions", "minimum_revisions_score", 55),
    ("strong revisions", "minimum_revisions_score", 70),
    ("improving margins", "minimum_quality_score", 70),
    ("profitable", "minimum_quality_score", 55),
    ("undervalued", "minimum_valuation_score", 70),
    ("cheap valuation", "minimum_valuation_score", 70),
    ("cheaper valuation", "minimum_valuation_score", 70),
    ("cheaper valuations", "minimum_valuation_score", 70),
    ("strong momentum", "minimum_momentum_score", 70),
    ("financially strong", "minimum_financial_strength_score", 70),
    ("strong ai demand", "minimum_ai_research_score", 70),
    ("dividend", "minimum_shareholder_return_score", 50),
    ("low debt", "maximum_debt_to_ebitda", 2.0),
)
