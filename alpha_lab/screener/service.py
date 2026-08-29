"""Present-day market discovery assembled from stored, attributable evidence."""

from datetime import date, datetime
import hashlib
import json

import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

from alpha_lab.config import Settings
from alpha_lab.ai import configured_ai_research_provider
from alpha_lab.ai.service import AIResearchService
from alpha_lab.database.models import (
    AIResearchAnalysis,
    BusinessTheme,
    Estimate,
    EthicalEvaluation,
    Fundamental,
    Price,
    Security,
)
from alpha_lab.ethics import EthicalClassificationService, load_ethics_policy
from alpha_lab.factors import percentile_scores
from alpha_lab.ratings import (
    calculate_coverage,
    calculate_quality_factors,
    calculate_revision_factors,
    calculate_valuation_factors,
)
from alpha_lab.strategy import HistoricalScoringService, coverage_interpretation
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.themes import derive_themes

CATEGORY_PROVENANCE = {
    "earnings_growth": "fundamental",
    "analyst_revisions": "estimate",
    "business_quality": "fundamental",
    "valuation": "fundamental",
    "momentum": "price",
    "financial_strength": "fundamental",
    "ai_research": "ai",
    "shareholder_return": "fundamental",
}


class LiveResearchRecord(BaseModel):
    ticker: str
    company: str | None
    price: float | None
    market_cap: float | None
    country: str | None
    exchange: str | None
    sector: str | None
    industry: str | None
    asset_type: str | None
    themes: list[str] = Field(default_factory=list)
    ethical_status: str
    data_quality_status: str
    overall_score: float | None
    overall_rank: int | None = None
    category_scores: dict[str, float | None]
    raw_metrics: dict[str, float | int | None]
    percentile_metrics: dict[str, float | None]
    overall_live_coverage: float
    quantitative_coverage: float
    ai_coverage: float
    historical_coverage: float
    confidence: str
    provenance: dict[str, dict]
    last_refreshed: datetime | None
    rating_version: str = "phase3-live-v2"
    configuration_hash: str
    evaluation_date: date


class MarketScreenerService:
    """Build current live records only; historical dates belong to HistoricalScoringService."""

    def __init__(self, engine: Engine, settings: Settings):
        self.engine, self.settings = engine, settings

    def build_live_records(self) -> list[LiveResearchRecord]:
        evaluation = date.today()
        EthicalClassificationService(
            self.engine, load_ethics_policy(self.settings.ethics_policy_path)
        ).ensure_all()
        AIResearchService(self.engine, configured_ai_research_provider()).ensure_all()
        with Session(self.engine) as metadata_session:
            metadata = list(
                metadata_session.scalars(select(Security).order_by(Security.ticker))
            )
        repository = Phase3Repository(self.engine)
        for security in metadata:
            repository.save_themes(
                security.ticker,
                derive_themes(
                    security.business_description,
                    security.metadata_source
                    or security.metadata_provider
                    or "stored-security-metadata",
                ),
            )
        base = {
            item.ticker: item
            for item in HistoricalScoringService(
                self.engine, self.settings
            ).score_universe_as_of(evaluation, min_score=0, minimum_coverage=0)
        }
        rows: dict[str, dict] = {}
        with Session(self.engine) as session:
            for security in session.scalars(select(Security).order_by(Security.ticker)):
                prices = list(
                    session.scalars(
                        select(Price)
                        .where(
                            Price.ticker == security.ticker, Price.date <= evaluation
                        )
                        .order_by(Price.date)
                    )
                )
                price = prices[-1] if prices else None
                fundamentals = list(
                    session.scalars(
                        select(Fundamental)
                        .where(
                            Fundamental.ticker == security.ticker,
                            or_(
                                Fundamental.publication_date.is_(None),
                                Fundamental.publication_date <= evaluation,
                            ),
                        )
                        .order_by(
                            Fundamental.period.desc(),
                            Fundamental.ingested_at.desc(),
                            Fundamental.id.desc(),
                        )
                    )
                )
                fundamental = fundamentals[0] if fundamentals else None
                prior = next(
                    (
                        item
                        for item in fundamentals
                        if fundamental and item.period < fundamental.period
                    ),
                    None,
                )
                estimates = list(
                    session.scalars(
                        select(Estimate)
                        .where(
                            Estimate.ticker == security.ticker,
                            Estimate.observation_date <= evaluation,
                        )
                        .order_by(Estimate.observation_date)
                    )
                )
                latest_period = max(
                    (item.fiscal_period for item in estimates), default=None
                )
                estimates = [
                    item for item in estimates if item.fiscal_period == latest_period
                ]
                latest_estimate = estimates[-1] if estimates else None
                estimate_frame = pd.DataFrame(
                    [
                        {
                            name: getattr(item, name)
                            for name in (
                                "observation_date",
                                "consensus_eps",
                                "consensus_revenue",
                                "analyst_count",
                                "estimate_dispersion",
                            )
                        }
                        for item in estimates
                    ]
                )
                revisions = calculate_revision_factors(estimate_frame, evaluation)
                current_price = (
                    price.adjusted_close
                    if price and price.adjusted_close is not None
                    else price.close
                    if price
                    else None
                )
                valuation = calculate_valuation_factors(
                    price=current_price,
                    shares=fundamental.shares_outstanding if fundamental else None,
                    eps=fundamental.eps if fundamental else None,
                    forward_eps=revisions["current_consensus_eps"],
                    revenue=fundamental.revenue if fundamental else None,
                    ebitda=fundamental.ebitda if fundamental else None,
                    free_cash_flow=fundamental.free_cash_flow if fundamental else None,
                    debt=fundamental.total_debt if fundamental else None,
                    cash=fundamental.cash if fundamental else None,
                )
                quality = calculate_quality_factors(
                    _fundamental_values(fundamental, security.market_cap),
                    _fundamental_values(prior, security.market_cap),
                )
                ethics = session.scalar(
                    select(EthicalEvaluation)
                    .where(EthicalEvaluation.ticker == security.ticker)
                    .order_by(
                        EthicalEvaluation.evaluated_at.desc(),
                        EthicalEvaluation.id.desc(),
                    )
                )
                ai = session.scalar(
                    select(AIResearchAnalysis)
                    .where(AIResearchAnalysis.ticker == security.ticker)
                    .order_by(AIResearchAnalysis.analysis_date.desc())
                )
                themes = list(
                    session.scalars(
                        select(BusinessTheme.theme)
                        .where(BusinessTheme.ticker == security.ticker)
                        .distinct()
                    )
                )
                quality_reason = _live_data_quality_reason(
                    prices, evaluation, self.settings
                )
                rows[security.ticker] = {
                    "security": security,
                    "price_row": price,
                    "fundamental": fundamental,
                    "price": current_price,
                    "valuation": valuation,
                    "quality": quality,
                    "revisions": revisions,
                    "estimate_row": latest_estimate,
                    "ethical_status": ethics.ethical_status if ethics else "REVIEW",
                    "ai": ai,
                    "themes": themes,
                    "quality_reason": quality_reason,
                }
        if not rows:
            return []
        raw = pd.DataFrame.from_dict(
            {
                ticker: {
                    **data["valuation"],
                    **data["revisions"],
                    **data["quality"],
                    **(base[ticker].raw_factors if ticker in base else {}),
                }
                for ticker, data in rows.items()
            },
            orient="index",
        )
        reference = [
            ticker
            for ticker, data in rows.items()
            if data["ethical_status"] == "PASS" and data["quality_reason"] == "valid"
        ]
        percentiles = _live_percentiles(raw, reference)
        results = [
            self._record(
                ticker, data, base.get(ticker), percentiles.loc[ticker], evaluation
            )
            for ticker, data in rows.items()
        ]
        ranked = sorted(
            (
                item
                for item in results
                if item.ethical_status == "PASS"
                and item.data_quality_status == "valid"
                and item.overall_score is not None
            ),
            key=lambda item: (-item.overall_score, item.ticker),
        )
        for rank, item in enumerate(ranked, 1):
            item.overall_rank = rank
        return sorted(
            results,
            key=lambda item: (
                item.overall_rank is None,
                item.overall_rank or 10**9,
                item.ticker,
            ),
        )

    def _record(
        self, ticker: str, data: dict, base, percentile: pd.Series, evaluation: date
    ) -> LiveResearchRecord:
        categories = {
            "earnings_growth": _mean(percentile, ["eps_growth", "revenue_growth"]),
            "analyst_revisions": _mean(
                percentile,
                [
                    "eps_revision_7d",
                    "eps_revision_30d",
                    "eps_revision_90d",
                    "revenue_revision_30d",
                ],
            ),
            "business_quality": _mean(
                percentile,
                [
                    "gross_margin",
                    "ebitda_margin",
                    "operating_margin",
                    "net_margin",
                    "roe",
                    "roa",
                    "fcf_conversion",
                ],
            ),
            "valuation": _mean(
                percentile,
                ["pe", "forward_pe", "price_sales", "ev_ebitda", "price_fcf"],
            ),
            "momentum": _mean(
                percentile,
                [
                    "return_1m",
                    "return_3m",
                    "return_6m",
                    "return_12m",
                    "momentum_12_1",
                    "distance_ma50",
                    "distance_ma200",
                ],
            ),
            "financial_strength": _mean(
                percentile,
                [
                    "net_debt",
                    "debt_ebitda",
                    "debt_equity",
                    "current_ratio",
                    "interest_coverage",
                    "cash_flow_to_debt",
                ],
            ),
            "ai_research": data["ai"].ai_rating if data["ai"] else None,
            "shareholder_return": _mean(
                percentile,
                ["dividend_yield", "buyback_yield", "total_shareholder_yield"],
            ),
        }
        coverage = calculate_coverage(
            categories,
            self.settings.rating_weights,
            ai_available=data["ai"] is not None,
            historical_available_weight=base.coverage if base else 0,
        )
        available_weight = sum(
            self.settings.rating_weights[name]
            for name, value in categories.items()
            if value is not None
        )
        score = (
            sum(
                float(value) * self.settings.rating_weights[name]
                for name, value in categories.items()
                if value is not None
            )
            / available_weight
            if available_weight
            else None
        )
        security, price, fundamental = (
            data["security"],
            data["price_row"],
            data["fundamental"],
        )
        refreshed = max(
            (
                value
                for value in [
                    security.metadata_updated_at,
                    price.ingested_at if price else None,
                    fundamental.ingested_at if fundamental else None,
                ]
                if value
            ),
            default=None,
        )
        return LiveResearchRecord(
            ticker=ticker,
            company=security.company_name,
            price=data["price"],
            market_cap=data["valuation"]["market_cap"] or security.market_cap,
            country=security.country,
            exchange=security.exchange,
            sector=security.sector,
            industry=security.industry,
            asset_type=security.asset_type,
            themes=data["themes"],
            ethical_status=data["ethical_status"],
            data_quality_status=data["quality_reason"],
            overall_score=round(score, 2) if score is not None else None,
            category_scores=categories,
            raw_metrics={
                **data["valuation"],
                **data["revisions"],
                **data["quality"],
                **(base.raw_factors if base else {}),
            },
            percentile_metrics={
                key: _optional(value) for key, value in percentile.items()
            },
            overall_live_coverage=coverage.overall_live,
            quantitative_coverage=coverage.quantitative,
            ai_coverage=coverage.ai_research,
            historical_coverage=coverage.historical,
            confidence=coverage_interpretation(
                score, coverage.overall_live, self.settings.coverage
            ),
            provenance={
                "price": _provenance(price),
                "fundamental": _provenance(fundamental),
                "estimate": _provenance(data["estimate_row"]),
                "ai": {
                    "provider": data["ai"].provider,
                    "model": data["ai"].model,
                    "document_ids": data["ai"].source_document_ids,
                    "analysis_date": data["ai"].analysis_date.isoformat(),
                }
                if data["ai"]
                else {},
                "evaluation": {
                    "as_of": evaluation.isoformat(),
                    "mode": "present-day-live",
                },
            },
            last_refreshed=refreshed,
            configuration_hash=_rating_hash(self.settings.rating_weights),
            evaluation_date=evaluation,
        )


def _live_data_quality_reason(
    prices: list[Price], evaluation: date, settings: Settings
) -> str:
    if not prices:
        return "missing price"
    if (evaluation - prices[-1].date).days > settings.data_quality["stale_price_days"]:
        return "stale price"
    if len(prices) < 22:
        return "insufficient history"
    volumes = [item.volume for item in prices[-20:] if item.volume is not None]
    minimum = max(
        settings.strategy.minimum_average_daily_volume,
        settings.data_quality.get("live_minimum_average_daily_volume", 100_000),
    )
    if minimum > 0 and (not volumes or sum(volumes) / len(volumes) < minimum):
        return "liquidity rule"
    return "valid"


def _live_percentiles(raw: pd.DataFrame, eligible: list[str]) -> pd.DataFrame:
    if not eligible:
        return pd.DataFrame(index=raw.index, columns=raw.columns, dtype=float)
    lower = {
        "pe",
        "forward_pe",
        "price_sales",
        "ev_ebitda",
        "ev_sales",
        "price_fcf",
        "estimate_dispersion",
        "net_debt",
        "debt_ebitda",
        "debt_equity",
        "volatility",
        "debt_to_ebitda",
    }
    result = percentile_scores(raw.loc[eligible], lower)
    for ticker in raw.index.difference(eligible):
        for column in raw.columns:
            value = raw.at[ticker, column]
            reference = raw.loc[eligible, column].dropna()
            if pd.isna(value) or reference.empty:
                result.at[ticker, column] = float("nan")
            elif column in lower:
                result.at[ticker, column] = float((reference >= value).mean() * 100)
            else:
                result.at[ticker, column] = float((reference <= value).mean() * 100)
    return result.reindex(raw.index)


def _mean(row: pd.Series, names: list[str]) -> float | None:
    values = [float(row[name]) for name in names if name in row and pd.notna(row[name])]
    return sum(values) / len(values) if values else None


def _optional(value) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _provenance(record) -> dict:
    if record is None:
        return {}
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in {
            "provider": getattr(record, "provider", None),
            "source": getattr(record, "source", None),
            "observation_date": getattr(record, "observation_date", None)
            or getattr(record, "date", None),
            "publication_date": getattr(record, "publication_date", None),
            "ingested_at": getattr(record, "ingested_at", None),
        }.items()
        if value is not None
    }


def _fundamental_values(record, market_cap: float | None) -> dict:
    if record is None:
        return {"market_cap": market_cap}
    names = [
        "revenue",
        "gross_profit",
        "ebitda",
        "ebit",
        "net_income",
        "eps",
        "free_cash_flow",
        "total_debt",
        "cash",
        "total_equity",
        "total_assets",
        "current_assets",
        "current_liabilities",
        "interest_expense",
        "dividends_paid",
        "share_repurchases",
    ]
    return {**{name: getattr(record, name) for name in names}, "market_cap": market_cap}


def _rating_hash(weights: dict[str, float]) -> str:
    return hashlib.sha256(
        json.dumps(weights, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
