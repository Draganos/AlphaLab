"""Present-day market discovery assembled from stored, attributable evidence."""

from datetime import date, datetime
import hashlib
import json
import math

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

CATEGORY_EVIDENCE_METRICS = {
    "earnings_growth": ("eps_growth", "revenue_growth"),
    "analyst_revisions": (
        "eps_revision_7d",
        "eps_revision_30d",
        "eps_revision_90d",
        "revenue_revision_7d",
        "revenue_revision_30d",
        "revenue_revision_90d",
    ),
    "business_quality": (
        "gross_margin",
        "ebitda_margin",
        "operating_margin",
        "net_margin",
        "roe",
        "roa",
        "fcf_conversion",
    ),
    "valuation": ("pe", "forward_pe", "price_sales", "ev_ebitda", "price_fcf"),
    "momentum": (
        "return_1m",
        "return_3m",
        "return_6m",
        "return_12m",
        "momentum_12_1",
        "distance_ma50",
        "distance_ma200",
    ),
    "financial_strength": (
        "net_debt",
        "debt_ebitda",
        "debt_equity",
        "current_ratio",
        "interest_coverage",
        "cash_flow_to_debt",
    ),
    "ai_research": (),
    "shareholder_return": (
        "dividend_yield",
        "buyback_yield",
        "total_shareholder_yield",
    ),
}

CATEGORY_MINIMUM_METRICS = {
    "earnings_growth": 1,
    "analyst_revisions": 1,
    "business_quality": 3,
    "valuation": 2,
    "momentum": 3,
    "financial_strength": 2,
    "ai_research": 1,
    "shareholder_return": 1,
}
CATEGORY_MANDATORY_METRICS = {
    "earnings_growth": (), "analyst_revisions": (), "business_quality": (),
    "valuation": (), "momentum": ("return_3m",), "financial_strength": (),
    "ai_research": (), "shareholder_return": (),
}
CATEGORY_METRIC_WEIGHTS = {
    category: {metric: 1.0 for metric in metrics}
    for category, metrics in CATEGORY_EVIDENCE_METRICS.items()
}

LIVE_FUNDAMENTAL_SOURCE_PRIORITY = {
    name: ("SECCompanyFactsProvider", "YFinanceProvider")
    for name in (
        "revenue", "gross_profit", "ebit", "net_income", "eps", "cash",
        "total_debt", "total_equity", "total_assets", "current_assets",
        "current_liabilities", "dividends_paid", "share_repurchases",
    )
}
LIVE_FUNDAMENTAL_SOURCE_PRIORITY.update({
    name: ("YFinanceProvider", "SECCompanyFactsProvider")
    for name in ("ebitda", "free_cash_flow", "shares_outstanding", "interest_expense")
})


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
    category_coverage: dict[str, float]
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

    def rebuild_current_research(self) -> list[LiveResearchRecord]:
        """Explicit write operation: derive, score, and persist current research."""
        records = self.build_live_records()
        Phase3Repository(self.engine).save_current_research(records)
        return records

    def read_current_research(self) -> list[LiveResearchRecord]:
        """Read-only UI path; never calls a provider, AI, ethics, or theme derivation."""
        _, payloads = Phase3Repository(self.engine).latest_current_payloads()
        return [LiveResearchRecord.model_validate(payload) for payload in payloads]

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
            theme_source = (
                security.metadata_source
                or security.metadata_provider
                or "stored-security-metadata"
            )
            derived_theme_source = f"auto-derived-business-metadata:{theme_source}"
            repository.save_themes(
                security.ticker,
                derive_themes(
                    security.business_description,
                    derived_theme_source,
                ),
                source=derived_theme_source,
                source_prefix="auto-derived-business-metadata:",
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
                usable_prices = [item for item in prices if _usable_close(item) is not None]
                price = usable_prices[-1] if usable_prices else None
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
                current_fundamentals, prior_fundamentals, field_provenance = (
                    _select_live_fundamental_values(fundamentals)
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
                estimates = _select_estimate_series(estimates, evaluation)
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
                current_price = _usable_close(price)
                valuation = calculate_valuation_factors(
                    price=current_price,
                    shares=current_fundamentals.get("shares_outstanding"),
                    eps=current_fundamentals.get("eps"),
                    forward_eps=revisions["current_consensus_eps"],
                    revenue=current_fundamentals.get("revenue"),
                    ebitda=current_fundamentals.get("ebitda"),
                    free_cash_flow=current_fundamentals.get("free_cash_flow"),
                    debt=current_fundamentals.get("total_debt"),
                    cash=current_fundamentals.get("cash"),
                )
                selected_market_cap = (
                    valuation["market_cap"]
                    if valuation["market_cap"] is not None
                    else security.market_cap
                )
                current_fundamentals["market_cap"] = selected_market_cap
                prior_fundamentals["market_cap"] = selected_market_cap
                quality = calculate_quality_factors(
                    current_fundamentals,
                    prior_fundamentals,
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
                ai_attributable = _ai_is_attributable(ai)
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
                    "fundamental_field_provenance": field_provenance,
                    "price": current_price,
                    "valuation": valuation,
                    "quality": quality,
                    "revisions": revisions,
                    "estimate_row": latest_estimate,
                    "ethical_status": ethics.ethical_status if ethics else "REVIEW",
                    "ai": ai,
                    "ai_attributable": ai_attributable,
                    "themes": themes,
                    "quality_reason": quality_reason,
                }
        if not rows:
            return []
        raw = pd.DataFrame.from_dict(
            {
                ticker: {
                    **_merge_live_raw(
                        base[ticker].raw_factors if ticker in base else {},
                        data["valuation"],
                        data["revisions"],
                        data["quality"],
                    ),
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
                ticker,
                data,
                base.get(ticker),
                raw.loc[ticker],
                percentiles.loc[ticker],
                evaluation,
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
        self,
        ticker: str,
        data: dict,
        base,
        raw_values: pd.Series,
        percentile: pd.Series,
        evaluation: date,
    ) -> LiveResearchRecord:
        categories = {
            "earnings_growth": _category_score(percentile, "earnings_growth"),
            "analyst_revisions": _category_score(percentile, "analyst_revisions"),
            "business_quality": _category_score(percentile, "business_quality"),
            "valuation": _category_score(percentile, "valuation"),
            "momentum": _category_score(percentile, "momentum"),
            "financial_strength": _category_score(percentile, "financial_strength"),
            "ai_research": data["ai"].ai_rating if data["ai_attributable"] else None,
            "shareholder_return": _category_score(percentile, "shareholder_return"),
        }
        category_coverage = _category_evidence_coverage(
            raw_values, ai_available=data["ai_attributable"]
        )
        coverage = calculate_coverage(
            category_coverage,
            self.settings.rating_weights,
            ai_available=data["ai_attributable"],
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
            market_cap=(
                data["valuation"]["market_cap"]
                if data["valuation"]["market_cap"] is not None
                else security.market_cap
            ),
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
            category_coverage=category_coverage,
            raw_metrics={key: _optional(value) for key, value in raw_values.items()},
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
                "fundamental": {
                    **_provenance(fundamental),
                    "fields": data["fundamental_field_provenance"],
                },
                "estimate": _provenance(data["estimate_row"]),
                "ai": {
                    "provider": data["ai"].provider,
                    "model": data["ai"].model,
                    "document_ids": data["ai"].source_document_ids,
                    "analysis_date": data["ai"].analysis_date.isoformat(),
                }
                if data["ai_attributable"]
                else {},
                "evaluation": {
                    "as_of": evaluation.isoformat(),
                    "mode": "present-day-live",
                },
                "metrics": _metric_provenance(
                    data["fundamental_field_provenance"],
                    _provenance(price),
                    _provenance(data["estimate_row"]),
                    {
                        "provider": data["ai"].provider,
                        "model": data["ai"].model,
                    }
                    if data["ai_attributable"]
                    else {},
                ),
            },
            last_refreshed=refreshed,
            configuration_hash=_rating_hash(self.settings.rating_weights),
            evaluation_date=evaluation,
        )


def _live_data_quality_reason(
    prices: list[Price], evaluation: date, settings: Settings
) -> str:
    usable = [item for item in prices if _usable_close(item) is not None]
    if not usable:
        return "missing price"
    if (evaluation - usable[-1].date).days > settings.data_quality["stale_price_days"]:
        return "stale price"
    if len(usable) < 22:
        return "insufficient history"
    volumes = [
        item.volume
        for item in usable[-20:]
        if item.volume is not None
        and math.isfinite(item.volume)
        and item.volume >= 0
    ]
    minimum = max(
        settings.strategy.minimum_average_daily_volume,
        settings.data_quality.get("live_minimum_average_daily_volume", 100_000),
    )
    if minimum > 0 and (not volumes or sum(volumes) / len(volumes) < minimum):
        return "liquidity rule"
    return "valid"


def _usable_close(price: Price | None) -> float | None:
    if price is None:
        return None
    for value in (price.adjusted_close, price.close):
        if value is not None and math.isfinite(value) and value > 0:
            return float(value)
    return None


def _ai_is_attributable(ai: AIResearchAnalysis | None) -> bool:
    return bool(
        ai
        and ai.source_document_ids
        and ai.evidence
        and ai.analyzed_document_ids
        and ai.input_fingerprint
        and ai.prompt_version
        and ai.model
    )


def _select_estimate_series(
    estimates: list[Estimate], evaluation: date
) -> list[Estimate]:
    """Choose the nearest non-expired forecast and one deterministic provider."""
    usable = [
        item
        for item in estimates
        if item.observation_date <= evaluation and item.fiscal_period >= evaluation
    ]
    if not usable:
        return []
    period = min(item.fiscal_period for item in usable)
    period_rows = [item for item in usable if item.fiscal_period == period]
    provider = max(
        {item.provider for item in period_rows},
        key=lambda name: (
            max(
                item.observation_date
                for item in period_rows
                if item.provider == name
            ),
            name,
        ),
    )
    return sorted(
        (item for item in period_rows if item.provider == provider),
        key=lambda item: (item.observation_date, item.id),
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
            elif (reference == value).any():
                tied = result.loc[reference.index[reference == value], column]
                result.at[ticker, column] = float(tied.mean())
            elif column in lower:
                result.at[ticker, column] = float((reference >= value).mean() * 100)
            else:
                result.at[ticker, column] = float((reference <= value).mean() * 100)
    return result.reindex(raw.index)


def _mean(row: pd.Series, names: tuple[str, ...]) -> float | None:
    values = [float(row[name]) for name in names if name in row and pd.notna(row[name])]
    return sum(values) / len(values) if values else None


def _category_score(row: pd.Series, category: str) -> float | None:
    names = CATEGORY_EVIDENCE_METRICS[category]
    mandatory = CATEGORY_MANDATORY_METRICS[category]
    if any(name not in row or pd.isna(row[name]) for name in mandatory):
        return None
    values = {
        name: float(row[name]) for name in names if name in row and pd.notna(row[name])
    }
    if len(values) < CATEGORY_MINIMUM_METRICS[category]:
        return None
    weights = CATEGORY_METRIC_WEIGHTS[category]
    available_weight = sum(weights[name] for name in values)
    return sum(value * weights[name] for name, value in values.items()) / available_weight


def _category_evidence_coverage(
    raw_values: pd.Series, *, ai_available: bool
) -> dict[str, float]:
    coverage: dict[str, float] = {}
    for category, metrics in CATEGORY_EVIDENCE_METRICS.items():
        if category == "ai_research":
            coverage[category] = 1.0 if ai_available else 0.0
            continue
        weights = CATEGORY_METRIC_WEIGHTS[category]
        available = sum(
            weights[metric]
            for metric in metrics
            if metric in raw_values and pd.notna(raw_values[metric])
        )
        expected = sum(weights.values())
        coverage[category] = available / expected if expected else 0.0
    return coverage


def _merge_live_raw(
    base_raw: dict,
    valuation: dict,
    revisions: dict,
    quality: dict,
) -> dict:
    """Use historical factors as a supplement; current live evidence always wins."""
    return {**base_raw, **valuation, **revisions, **quality}


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


def _select_live_fundamental_values(
    records: list[Fundamental],
) -> tuple[dict, dict, dict[str, dict]]:
    """Select every field by explicit provider priority and keep growth provider-consistent."""
    current: dict[str, float | None] = {}
    prior: dict[str, float | None] = {}
    provenance: dict[str, dict] = {}
    for field, priority in LIVE_FUNDAMENTAL_SOURCE_PRIORITY.items():
        usable = [
            record
            for record in records
            if _finite_field(getattr(record, field, None), positive=field == "shares_outstanding")
        ]
        providers = {record.provider for record in usable}
        provider = next((name for name in priority if name in providers), None)
        if provider is None and providers:
            provider = sorted(providers)[0]
        provider_rows = sorted(
            (record for record in usable if record.provider == provider),
            key=lambda record: (record.period, record.publication_date or date.min, record.id),
        )
        if not provider_rows:
            current[field] = None
            prior[field] = None
            continue
        selected = provider_rows[-1]
        earlier = next(
            (record for record in reversed(provider_rows[:-1]) if record.period < selected.period),
            None,
        )
        current[field] = float(getattr(selected, field))
        prior[field] = float(getattr(earlier, field)) if earlier is not None else None
        provenance[field] = {
            **_provenance(selected),
            "period": selected.period.isoformat(),
            "provider_priority": list(priority),
        }
    return current, prior, provenance


def _finite_field(value, *, positive: bool = False) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and (not positive or number > 0)


def _rating_hash(weights: dict[str, float]) -> str:
    return hashlib.sha256(
        json.dumps(weights, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _metric_provenance(
    fundamental: dict[str, dict], price: dict, estimate: dict, ai: dict
) -> dict[str, dict]:
    mapping = {
        "eps_growth": "eps", "revenue_growth": "revenue",
        "gross_margin": "gross_profit", "ebitda_margin": "ebitda",
        "operating_margin": "ebit", "net_margin": "net_income",
        "fcf_margin": "free_cash_flow", "roe": "net_income", "roa": "net_income",
        "fcf_conversion": "free_cash_flow", "pe": "eps", "price_sales": "revenue",
        "ev_ebitda": "ebitda", "price_fcf": "free_cash_flow",
        "fcf_yield": "free_cash_flow", "earnings_yield": "eps",
        "net_debt": "total_debt", "debt_ebitda": "total_debt",
        "debt_equity": "total_debt", "current_ratio": "current_assets",
        "interest_coverage": "interest_expense", "cash_flow_to_debt": "free_cash_flow",
        "dividend_yield": "dividends_paid", "buyback_yield": "share_repurchases",
        "total_shareholder_yield": "dividends_paid",
    }
    result = {metric: fundamental.get(field, {}) for metric, field in mapping.items()}
    result.update({metric: price for metric in CATEGORY_EVIDENCE_METRICS["momentum"]})
    result.update({metric: estimate for metric in CATEGORY_EVIDENCE_METRICS["analyst_revisions"]})
    result["forward_pe"] = estimate
    result["ai_research"] = ai
    return result
