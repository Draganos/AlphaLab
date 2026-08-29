"""Deterministic long-only portfolio construction with explicit exclusions."""

from dataclasses import dataclass, field
import math


@dataclass(frozen=True)
class Candidate:
    ticker: str
    score: float | None
    coverage: float
    sector: str | None = None
    volatility: float | None = None
    has_price: bool = True
    stale_price: bool = False
    sufficient_history: bool = True
    liquid: bool = True
    ethical_status: str = "PASS"


@dataclass
class PortfolioTargets:
    weights: dict[str, float] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def cash_weight(self) -> float:
        return max(0.0, 1.0 - sum(self.weights.values()))


def construct_portfolio(
    candidates: list[Candidate],
    *,
    method: str,
    min_score: float,
    minimum_coverage: float,
    min_positions: int,
    max_positions: int,
    max_position: float,
    max_sector: float | None,
    ethical_filter_enabled: bool = False,
    allowed_ethical_statuses: tuple[str, ...] = ("PASS",),
) -> PortfolioTargets:
    """Select and weight candidates without forcing the configured minimum count."""
    if method not in {"equal", "score", "inverse_volatility"}:
        raise ValueError(f"Unsupported weighting method: {method}")
    result = PortfolioTargets()
    eligible: list[Candidate] = []
    for candidate in candidates:
        reason = _eligibility_reason(
            candidate,
            min_score,
            minimum_coverage,
            ethical_filter_enabled,
            allowed_ethical_statuses,
        )
        if reason:
            result.excluded[candidate.ticker] = reason
        else:
            eligible.append(candidate)
    eligible.sort(key=lambda item: (-float(item.score), item.ticker))
    selected = eligible[:max_positions]
    for candidate in eligible[max_positions:]:
        result.excluded[candidate.ticker] = "position limit"
    if len(selected) < min_positions:
        result.notes.append(
            f"Only {len(selected)} of {min_positions} desired positions qualified; remainder is cash"
        )
    raw = _raw_weights(selected, method)
    sector_used: dict[str, float] = {}
    for candidate in selected:
        weight = min(raw[candidate.ticker], max_position)
        sector = candidate.sector or "Unknown"
        if max_sector is not None:
            room = max(0.0, max_sector - sector_used.get(sector, 0.0))
            weight = min(weight, room)
        if weight <= 0:
            result.excluded[candidate.ticker] = "sector constraint"
            continue
        result.weights[candidate.ticker] = weight
        sector_used[sector] = sector_used.get(sector, 0.0) + weight
    return result


def _eligibility_reason(
    candidate: Candidate,
    min_score: float,
    minimum_coverage: float,
    ethical_filter_enabled: bool = False,
    allowed_ethical_statuses: tuple[str, ...] = ("PASS",),
) -> str | None:
    if (
        ethical_filter_enabled
        and candidate.ethical_status not in allowed_ethical_statuses
    ):
        return f"ethical status {candidate.ethical_status.lower()}"
    if not candidate.has_price:
        return "missing price"
    if candidate.stale_price:
        return "stale price"
    if not candidate.sufficient_history:
        return "insufficient history"
    if not candidate.liquid:
        return "liquidity rule"
    if candidate.coverage < minimum_coverage:
        return "insufficient coverage"
    if candidate.score is None or candidate.score < min_score:
        return "score below threshold"
    return None


def _raw_weights(selected: list[Candidate], method: str) -> dict[str, float]:
    if method == "equal":
        values = {candidate.ticker: 1.0 for candidate in selected}
    elif method == "score":
        values = {
            candidate.ticker: max(float(candidate.score or 0), 0.0)
            for candidate in selected
        }
    else:
        values = {
            candidate.ticker: (
                1 / candidate.volatility
                if candidate.volatility is not None
                and math.isfinite(candidate.volatility)
                and candidate.volatility > 0
                else 0.0
            )
            for candidate in selected
        }
    total = sum(values.values())
    return (
        {ticker: value / total for ticker, value in values.items()}
        if total > 0
        else {candidate.ticker: 0.0 for candidate in selected}
    )
