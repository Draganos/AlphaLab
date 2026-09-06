"""Analyst Consensus: current professional-analyst opinion.

Deliberately separate from Analyst Revisions (``alpha_lab.screener.service``'s
existing ``analyst_revisions`` category, sourced from EPS/revenue estimate
history in ``alpha_lab.database.models.Estimate``). Analyst Consensus is
"what do analysts currently think" (buy/hold/sell counts, price targets);
Analyst Revisions is "how has analyst opinion changed over time". Neither
feeds the other, and this module never touches the ``analyst_revisions``
category or the ``Estimate`` table.

This is a separate research domain from AlphaLab's 0-100 fundamental score.
Its rating is never blended into ``StockResearch.overall_score``.

Methodology (``ANALYST_CONSENSUS_METHODOLOGY_VERSION``): the displayed
rating is a deterministic ordinal-weighted mean of the five
recommendationTrend counts (Strong Buy=+2, Buy=+1, Hold=0, Sell=-1,
Strong Sell=-2), divided by total analysts, mapped to a band by fixed
thresholds. Never computed if any of the five counts is missing (as opposed
to zero, which is a real, present value) -- a missing category makes the
weighted mean meaningless, not merely approximate, so the result is
``AnalystRating.REVIEW`` rather than treating the missing count as zero.
"""

from datetime import date
from enum import StrEnum
import math

from pydantic import BaseModel, Field

ANALYST_CONSENSUS_METHODOLOGY_VERSION = "analyst-consensus-v1"

# Coverage is the fraction of these nine documented evidence fields that are
# present (not None). total_analysts, target_current, and upside_to_mean are
# derived from these, not counted separately, so coverage cannot be inflated
# by fields that are never independent evidence.
_COVERAGE_FIELDS = (
    "strong_buy", "buy", "hold", "sell", "strong_sell",
    "target_low", "target_mean", "target_median", "target_high",
)

_ORDINAL_WEIGHTS = {"strong_buy": 2, "buy": 1, "hold": 0, "sell": -1, "strong_sell": -2}


class AnalystRating(StrEnum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"
    # Data exists but is not safe to rate: a recommendation category is
    # missing (not zero), or confirmed total analyst coverage is zero.
    REVIEW = "REVIEW"


# Explicit, versioned thresholds on the -2..+2 weighted-mean scale. Do not
# change without bumping ANALYST_CONSENSUS_METHODOLOGY_VERSION -- a stored
# historical AnalystConsensus keeps whatever version it was computed under,
# so old snapshots stay interpretable under their own methodology.
_RATING_THRESHOLDS_V1: tuple[tuple[float, AnalystRating], ...] = (
    (1.5, AnalystRating.STRONG_BUY),
    (0.5, AnalystRating.BUY),
    (-0.5, AnalystRating.NEUTRAL),
    (-1.5, AnalystRating.SELL),
)
_RATING_FLOOR = AnalystRating.STRONG_SELL


class AnalystConsensus(BaseModel):
    ticker: str
    rating: AnalystRating | None = None
    rating_score: float | None = Field(None, ge=-2, le=2)
    strong_buy: int | None = Field(None, ge=0)
    buy: int | None = Field(None, ge=0)
    hold: int | None = Field(None, ge=0)
    sell: int | None = Field(None, ge=0)
    strong_sell: int | None = Field(None, ge=0)
    total_analysts: int | None = Field(None, ge=0)
    target_current: float | None = Field(None, gt=0)
    target_low: float | None = Field(None, gt=0)
    target_mean: float | None = Field(None, gt=0)
    target_median: float | None = Field(None, gt=0)
    target_high: float | None = Field(None, gt=0)
    upside_to_mean: float | None = None
    # The date AlphaLab fetched this observation -- never a Yahoo-reported
    # "as of" date, since the recommendationTrend/financialData endpoints
    # this is built from do not expose one. Never presented as if Yahoo
    # itself timestamped the observation.
    as_of: date | None = None
    source: str
    source_version: str = ANALYST_CONSENSUS_METHODOLOGY_VERSION
    coverage: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


def compute_rating_score(
    *,
    strong_buy: int | None,
    buy: int | None,
    hold: int | None,
    sell: int | None,
    strong_sell: int | None,
) -> float | None:
    """Deterministic ordinal-weighted mean, or None if unsafe to compute.

    None (not zero) whenever any category is missing -- a missing category
    is not the same as zero analysts in it, and silently treating it as
    zero would distort the denominator and mislead the resulting rating.
    Also None when every category is present but sums to zero: a confirmed
    absence of analyst coverage is not "neutral", it is "no consensus".
    """
    counts = {
        "strong_buy": strong_buy,
        "buy": buy,
        "hold": hold,
        "sell": sell,
        "strong_sell": strong_sell,
    }
    if any(value is None for value in counts.values()):
        return None
    total = sum(counts.values())
    if total <= 0:
        return None
    weighted = sum(_ORDINAL_WEIGHTS[name] * value for name, value in counts.items())
    return weighted / total


def map_score_to_rating(score: float | None) -> AnalystRating:
    """REVIEW when `score` is None -- never silently defaults to NEUTRAL."""
    if score is None:
        return AnalystRating.REVIEW
    for threshold, rating in _RATING_THRESHOLDS_V1:
        if score >= threshold:
            return rating
    return _RATING_FLOOR


def compute_upside_to_mean(target_mean: float | None, target_current: float | None) -> float | None:
    if target_mean is None or target_current is None:
        return None
    if not (math.isfinite(target_mean) and math.isfinite(target_current)):
        return None
    if target_current == 0:
        return None
    return (target_mean / target_current) - 1


def build_analyst_consensus(
    *,
    ticker: str,
    strong_buy: int | None,
    buy: int | None,
    hold: int | None,
    sell: int | None,
    strong_sell: int | None,
    target_current: float | None,
    target_low: float | None,
    target_mean: float | None,
    target_median: float | None,
    target_high: float | None,
    as_of: date | None,
    source: str,
) -> AnalystConsensus:
    """Pure, deterministic construction of the canonical AnalystConsensus.

    Takes already-fetched raw fields (see ``YFinanceProvider.get_analyst_consensus``
    for where they come from) -- this function itself never calls a provider
    and is fully unit-testable without network access.
    """
    score = compute_rating_score(
        strong_buy=strong_buy, buy=buy, hold=hold, sell=sell, strong_sell=strong_sell
    )
    rating = map_score_to_rating(score)
    counts = {"strong_buy": strong_buy, "buy": buy, "hold": hold, "sell": sell, "strong_sell": strong_sell}
    total_analysts = (
        sum(counts.values()) if all(value is not None for value in counts.values()) else None
    )
    upside = compute_upside_to_mean(target_mean, target_current)

    values = {
        "strong_buy": strong_buy, "buy": buy, "hold": hold, "sell": sell, "strong_sell": strong_sell,
        "target_low": target_low, "target_mean": target_mean,
        "target_median": target_median, "target_high": target_high,
    }
    present = sum(1 for field in _COVERAGE_FIELDS if values[field] is not None)
    coverage = present / len(_COVERAGE_FIELDS)
    # Confidence mirrors coverage but also requires a ratable consensus and a
    # non-trivial analyst count -- a technically "full coverage" reading of
    # zero analysts, or one that couldn't be rated, is not confidently
    # actionable even though every field happened to be present.
    confidence = coverage
    if rating == AnalystRating.REVIEW:
        confidence *= 0.5
    elif total_analysts is not None and total_analysts < 3:
        confidence *= 0.75

    evidence: list[str] = []
    if total_analysts is not None:
        evidence.append(f"{total_analysts} analyst(s) covering {ticker}")
    if score is not None:
        evidence.append(f"weighted consensus score = {score:.2f} (-2..+2 scale)")
    if upside is not None:
        evidence.append(f"upside to mean target = {upside:+.1%}")

    return AnalystConsensus(
        ticker=ticker.upper(),
        rating=rating,
        rating_score=score,
        strong_buy=strong_buy,
        buy=buy,
        hold=hold,
        sell=sell,
        strong_sell=strong_sell,
        total_analysts=total_analysts,
        target_current=target_current,
        target_low=target_low,
        target_mean=target_mean,
        target_median=target_median,
        target_high=target_high,
        upside_to_mean=upside,
        as_of=as_of,
        source=source,
        source_version=ANALYST_CONSENSUS_METHODOLOGY_VERSION,
        coverage=coverage,
        confidence=confidence,
        evidence=evidence,
    )
