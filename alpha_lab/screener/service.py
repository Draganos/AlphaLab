"""Live market discovery assembled from stored, attributable evidence."""

from datetime import date, datetime
import hashlib
import json
import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.config import Settings
from alpha_lab.database.models import (
    AIResearchAnalysis,
    BusinessTheme,
    Estimate,
    EthicalEvaluation,
    Fundamental,
    Price,
    Security,
)
from alpha_lab.factors import percentile_scores
from alpha_lab.ratings import (
    calculate_coverage,
    calculate_quality_factors,
    calculate_revision_factors,
    calculate_valuation_factors,
)
from alpha_lab.strategy import HistoricalScoringService, coverage_interpretation


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
    overall_score: float | None
    overall_rank: int | None = None
    category_scores: dict[str, float | None]
    raw_metrics: dict[str, float | int | None]
    overall_live_coverage: float
    quantitative_coverage: float
    ai_coverage: float
    historical_coverage: float
    confidence: str
    provenance: dict[str, dict]
    last_refreshed: datetime | None
    rating_version: str = "phase3-live-v1"
    configuration_hash: str
    evaluation_date: date


class MarketScreenerService:
    def __init__(self, engine: Engine, settings: Settings):
        self.engine, self.settings = engine, settings

    def build_live_records(self, as_of: date | None = None) -> list[LiveResearchRecord]:
        evaluation = as_of or date.today()
        base = {
            item.ticker: item
            for item in HistoricalScoringService(
                self.engine, self.settings
            ).score_universe_as_of(evaluation, min_score=0, minimum_coverage=0)
        }
        rows: dict[str, dict] = {}
        with Session(self.engine) as session:
            for security in session.scalars(select(Security).order_by(Security.ticker)):
                price = session.scalar(
                    select(Price)
                    .where(Price.ticker == security.ticker)
                    .order_by(Price.date.desc(), Price.id.desc())
                )
                fundamental = session.scalar(
                    select(Fundamental)
                    .where(Fundamental.ticker == security.ticker)
                    .order_by(Fundamental.period.desc(), Fundamental.ingested_at.desc())
                )
                fundamental_history = list(
                    session.scalars(
                        select(Fundamental)
                        .where(Fundamental.ticker == security.ticker)
                        .order_by(
                            Fundamental.period.desc(), Fundamental.ingested_at.desc()
                        )
                    )
                )
                estimate_rows = list(
                    session.scalars(
                        select(Estimate)
                        .where(Estimate.ticker == security.ticker)
                        .order_by(Estimate.observation_date)
                    )
                )
                latest_period = max(
                    (item.fiscal_period for item in estimate_rows), default=None
                )
                estimate_rows = [
                    item
                    for item in estimate_rows
                    if item.fiscal_period == latest_period
                ]
                estimate_frame = pd.DataFrame(
                    [
                        {
                            column: getattr(item, column)
                            for column in [
                                "observation_date",
                                "consensus_eps",
                                "consensus_revenue",
                                "analyst_count",
                                "estimate_dispersion",
                            ]
                        }
                        for item in estimate_rows
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
                    _fundamental_values(
                        fundamental_history[1]
                        if len(fundamental_history) > 1
                        else None,
                        security.market_cap,
                    ),
                )
                ethics = session.scalar(
                    select(EthicalEvaluation)
                    .where(EthicalEvaluation.ticker == security.ticker)
                    .order_by(EthicalEvaluation.evaluated_at.desc())
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
                rows[security.ticker] = {
                    "security": security,
                    "price_row": price,
                    "fundamental": fundamental,
                    "price": current_price,
                    "valuation": valuation,
                    "quality": quality,
                    "revisions": revisions,
                    "ethical_status": ethics.ethical_status if ethics else "UNKNOWN",
                    "ai": ai,
                    "themes": themes,
                }
        raw = pd.DataFrame.from_dict(
            {
                ticker: {**data["valuation"], **data["revisions"], **data["quality"]}
                for ticker, data in rows.items()
            },
            orient="index",
        )
        eligible = [
            ticker for ticker, data in rows.items() if data["ethical_status"] == "PASS"
        ]
        percentiles = _live_percentiles(raw, eligible)
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
                if item.ethical_status == "PASS" and item.overall_score is not None
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
        revision_score = _mean(
            percentile,
            [
                "eps_revision_7d",
                "eps_revision_30d",
                "eps_revision_90d",
                "revenue_revision_30d",
            ],
        )
        valuation_score = _mean(
            percentile, ["pe", "forward_pe", "price_sales", "ev_ebitda", "price_fcf"]
        )
        quality_score = _mean(
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
        )
        strength_score = _mean(
            percentile,
            [
                "net_debt",
                "debt_ebitda",
                "debt_equity",
                "current_ratio",
                "interest_coverage",
                "cash_flow_to_debt",
            ],
        )
        shareholder_score = _mean(
            percentile, ["dividend_yield", "buyback_yield", "total_shareholder_yield"]
        )
        categories = {
            "earnings_growth": base.category_scores.get("earnings") if base else None,
            "analyst_revisions": revision_score,
            "business_quality": quality_score,
            "valuation": valuation_score,
            "momentum": base.category_scores.get("momentum") if base else None,
            "financial_strength": strength_score,
            "ai_research": data["ai"].ai_rating if data["ai"] else None,
            "shareholder_return": shareholder_score,
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
            overall_score=round(score, 2) if score is not None else None,
            category_scores=categories,
            raw_metrics={
                **data["valuation"],
                **data["revisions"],
                **data["quality"],
                **(base.raw_factors if base else {}),
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
                "evaluation": {"as_of": evaluation.isoformat()},
            },
            last_refreshed=refreshed,
            configuration_hash=_rating_hash(self.settings.rating_weights),
            evaluation_date=evaluation,
        )


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
    }
    valid = percentile_scores(raw.loc[eligible], lower)
    return valid.reindex(raw.index)


def _mean(row: pd.Series, names: list[str]) -> float | None:
    values = [float(row[name]) for name in names if name in row and pd.notna(row[name])]
    return sum(values) / len(values) if values else None


def _provenance(record) -> dict:
    if record is None:
        return {}
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in {
            "provider": getattr(record, "provider", None),
            "source": getattr(record, "source", None),
            "observation_date": getattr(record, "date", None),
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
    payload = json.dumps(weights, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
