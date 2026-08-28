"""Shared point-in-time universe scoring for the screener and backtester."""

from dataclasses import dataclass
from datetime import date
from typing import Any
import hashlib
import json
import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.config import Settings
from alpha_lab.database.models import Price, Security
from alpha_lab.database.queries import latest_fundamentals_as_of
from alpha_lab.factors import calculate_factors, percentile_scores
from alpha_lab.factors.engine import FACTOR_VERSION
from alpha_lab.portfolio import Candidate
from alpha_lab.strategy.scoring import composite_score


@dataclass(frozen=True)
class HistoricalScore:
    ticker: str
    company: str | None
    sector: str | None
    raw_factors: dict[str, float]
    percentile_factors: dict[str, float | None]
    category_scores: dict[str, float | None]
    score: float | None
    coverage: float
    confidence_label: str
    eligible: bool
    exclusion_reason: str | None
    score_version: str
    config_hash: str
    evaluation_date: date

    def candidate(self) -> Candidate:
        return Candidate(self.ticker, self.score, self.coverage, self.sector,
                         self.raw_factors.get("volatility"),
                         has_price=pd.notna(self.raw_factors.get("last_price")),
                         stale_price=self.exclusion_reason == "stale price",
                         sufficient_history=self.exclusion_reason != "insufficient history",
                         liquid=self.exclusion_reason != "liquidity rule")


class HistoricalScoringService:
    def __init__(self, engine: Engine, settings: Settings):
        self.engine, self.settings = engine, settings

    def score_universe_as_of(self, evaluation_date: date, tickers: list[str] | tuple[str, ...] | None = None,
                             *, min_score: float | None = None,
                             minimum_coverage: float | None = None) -> list[HistoricalScore]:
        symbols = {ticker.upper() for ticker in tickers} if tickers else None
        raw: dict[str, dict[str, float]] = {}
        metadata: dict[str, tuple[str | None, str | None, str | None]] = {}
        with Session(self.engine) as session:
            securities = session.scalars(select(Security).order_by(Security.ticker)).all()
            for security in securities:
                if symbols is not None and security.ticker not in symbols:
                    continue
                prices = session.scalars(select(Price).where(
                    Price.ticker == security.ticker, Price.date <= evaluation_date).order_by(Price.date)).all()
                fundamentals = latest_fundamentals_as_of(session, security.ticker, evaluation_date)
                series = pd.Series({item.date: item.adjusted_close or item.close for item in prices}, dtype=float)
                frame = pd.DataFrame([{name: getattr(item, name) for name in
                    ["period", "publication_date", "ingested_at", "revenue", "ebitda", "net_income", "eps",
                     "free_cash_flow", "total_debt", "cash", "total_equity"]} for item in fundamentals])
                raw[security.ticker] = calculate_factors(series, frame, evaluation_date)
                recent_volume = [item.volume for item in prices[-20:] if item.volume is not None]
                raw[security.ticker]["average_volume_20d"] = (
                    sum(recent_volume) / len(recent_volume) if recent_volume else float("nan"))
                latest_date = prices[-1].date if prices else None
                metadata[security.ticker] = (security.company_name, security.sector,
                    self._pre_score_exclusion(len(prices), latest_date, evaluation_date,
                                              raw[security.ticker]["average_volume_20d"]))
        if not raw:
            return []
        valid_tickers = [ticker for ticker, details in metadata.items() if details[2] is None]
        raw_frame = pd.DataFrame.from_dict(raw, orient="index")
        percentiles = _percentiles_against_valid_universe(raw_frame, valid_tickers)
        results: list[HistoricalScore] = []
        score_floor = self.settings.strategy.min_score if min_score is None else min_score
        coverage_floor = (self.settings.strategy.minimum_data_coverage
                          if minimum_coverage is None else minimum_coverage)
        for ticker in sorted(raw):
            percentile_row = (percentiles.loc[ticker] if ticker in percentiles.index
                              else pd.Series(index=raw_frame.columns, dtype=float))
            category = self._categories(percentile_row)
            composite = composite_score(category, self.settings.weights, evaluation_date, self.settings.coverage)
            pre_reason = metadata[ticker][2]
            reason = pre_reason or ("insufficient coverage" if composite.coverage < coverage_floor else
                                    "score below threshold" if composite.score is None or composite.score < score_floor
                                    else None)
            results.append(HistoricalScore(ticker, metadata[ticker][0], metadata[ticker][1], raw[ticker],
                {key: _optional(value) for key, value in percentile_row.items()}, category,
                composite.score, composite.coverage, composite.confidence_label, reason is None, reason,
                composite.score_version, _historical_hash(composite.config_hash, score_floor, coverage_floor),
                evaluation_date))
        return results

    def _pre_score_exclusion(self, count: int, latest: date | None, evaluation: date,
                             average_volume: float) -> str | None:
        if latest is None:
            return "missing price"
        if (evaluation - latest).days > self.settings.data_quality["stale_price_days"]:
            return "stale price"
        if count < 22:
            return "insufficient history"
        minimum_volume = self.settings.strategy.minimum_average_daily_volume
        if minimum_volume > 0 and (pd.isna(average_volume) or average_volume < minimum_volume):
            return "liquidity rule"
        return None

    @staticmethod
    def _categories(row: pd.Series) -> dict[str, float | None]:
        def mean(names: list[str]) -> float | None:
            values = [row.get(name) for name in names]
            finite = [float(value) for value in values if pd.notna(value)]
            return sum(finite) / len(finite) if finite else None
        return {"earnings": _optional(row.get("eps_yoy_growth")), "revisions": None,
                "fundamentals": mean(["revenue_yoy_growth", "ebitda_margin", "net_margin", "roe"]),
                "valuation": None,
                "momentum": mean(["return_3m", "return_6m", "momentum_12_1", "distance_ma200"]),
                "balance_sheet": mean(["debt_to_ebitda"]), "ai": None, "dividend": None}


def _optional(value: Any) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _percentiles_against_valid_universe(raw: pd.DataFrame, valid_tickers: list[str]) -> pd.DataFrame:
    """Score excluded rows against—but never as part of—the valid reference distribution."""
    if not valid_tickers:
        return pd.DataFrame(index=raw.index, columns=raw.columns, dtype=float)
    lower_is_better = {"volatility", "debt_to_ebitda"}
    result = percentile_scores(raw.loc[valid_tickers], lower_is_better)
    for ticker in raw.index.difference(valid_tickers):
        for column in raw.columns:
            value = raw.at[ticker, column]
            reference = raw.loc[valid_tickers, column].dropna()
            if pd.isna(value) or reference.empty:
                result.at[ticker, column] = float("nan")
            elif column in lower_is_better:
                result.at[ticker, column] = float((reference >= value).mean() * 100)
            else:
                result.at[ticker, column] = float((reference <= value).mean() * 100)
    return result.reindex(raw.index)


def _historical_hash(composite_hash: str, min_score: float, minimum_coverage: float) -> str:
    payload = json.dumps({"composite_hash": composite_hash, "factor_version": FACTOR_VERSION,
                          "min_score": min_score, "minimum_coverage": minimum_coverage},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
