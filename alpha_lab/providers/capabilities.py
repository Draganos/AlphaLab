"""Deterministic provider capabilities and field/domain source selection policy."""

from enum import StrEnum


class Capability(StrEnum):
    RELIABLE_CURRENT = "RELIABLE_CURRENT"
    RELIABLE_POINT_IN_TIME = "RELIABLE_POINT_IN_TIME"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"


CAPABILITY_FIELDS = (
    "universe.ticker", "universe.name", "universe.exchange",
    "universe.security_type", "universe.active_status", "universe.common_equity",
    "prices.open", "prices.high", "prices.low", "prices.close",
    "prices.adjusted", "prices.volume", "prices.dividends", "prices.splits",
    "fundamentals.revenue", "fundamentals.eps", "fundamentals.net_income",
    "fundamentals.ebitda", "fundamentals.margins", "fundamentals.roe",
    "fundamentals.roic", "fundamentals.cash", "fundamentals.debt",
    "fundamentals.shares_outstanding", "valuation.market_cap",
    "valuation.enterprise_value", "valuation.pe", "valuation.ev_ebitda",
    "valuation.price_sales", "valuation.price_book", "estimates.fiscal_period",
    "estimates.eps", "estimates.revenue", "estimates.analyst_count",
    "estimates.timestamp", "estimates.dispersion", "shareholder.dividends",
    "shareholder.buybacks", "shareholder.share_count_change",
    "classification.sector", "classification.industry",
    "classification.description", "classification.activities",
    "documents.sec_filings", "documents.earnings_releases",
    "documents.official_announcements", "documents.news",
    "analyst_recommendations", "analyst_recommendation_summary",
    "analyst_price_targets", "technical_price_history",
)


PROVIDER_CAPABILITIES: dict[str, dict[str, Capability]] = {
    "NasdaqTraderUniverseProvider": {
        "universe.ticker": Capability.RELIABLE_CURRENT,
        "universe.name": Capability.RELIABLE_CURRENT,
        "universe.exchange": Capability.RELIABLE_CURRENT,
        "universe.security_type": Capability.PARTIAL,
        "universe.active_status": Capability.RELIABLE_CURRENT,
        "universe.common_equity": Capability.PARTIAL,
    },
    "YFinanceProvider": {
        "prices.ohlcv": Capability.RELIABLE_CURRENT,
        "prices.adjusted_close": Capability.RELIABLE_CURRENT,
        "prices.dividends_splits": Capability.PARTIAL,
        "fundamentals.current": Capability.PARTIAL,
        "fundamentals.point_in_time": Capability.UNSUPPORTED,
        "valuation.current": Capability.PARTIAL,
        "business_classification": Capability.PARTIAL,
        "estimates.current": Capability.PARTIAL,
        "estimates.history": Capability.UNSUPPORTED,
        # Analyst Consensus (recommendationTrend / financialData targets) --
        # distinct from "estimates.*" above, which is EPS/revenue consensus
        # for Analyst Revisions, not buy/hold/sell opinion or price targets.
        # Coverage is real but inconsistent: large, well-covered names return
        # a full recommendationTrend row; thinly-covered tickers often don't.
        "analyst_recommendations": Capability.PARTIAL,
        "analyst_recommendation_summary": Capability.PARTIAL,
        "analyst_price_targets": Capability.PARTIAL,
        # AlphaLab computes its own technical indicators from stored OHLCV
        # history that YFinance already supplies reliably (see prices.* above)
        # -- this capability is about that underlying history being
        # available at all, not about any one ticker having enough of it for
        # a specific indicator (that is a per-indicator coverage check, not
        # a capability fact).
        "technical_price_history": Capability.RELIABLE_CURRENT,
    },
    "SECCompanyFactsProvider": {
        "fundamentals.reported": Capability.RELIABLE_POINT_IN_TIME,
        "documents.filings": Capability.UNSUPPORTED,
        "fundamentals.ebitda": Capability.UNSUPPORTED,
        "fundamentals.free_cash_flow": Capability.UNSUPPORTED,
        "estimates": Capability.UNSUPPORTED,
        "news": Capability.UNSUPPORTED,
    },
    "AlphaLabEstimateSnapshots": {
        "estimates.history": Capability.RELIABLE_POINT_IN_TIME,
    },
}

PROVIDER_CAPABILITIES["NasdaqTraderUniverseProvider"].update({
    field: Capability.RELIABLE_CURRENT
    for field in ("universe.ticker", "universe.name", "universe.exchange", "universe.active_status")
})
PROVIDER_CAPABILITIES["NasdaqTraderUniverseProvider"].update({
    field: Capability.PARTIAL
    for field in ("universe.security_type", "universe.common_equity")
})
PROVIDER_CAPABILITIES["YFinanceProvider"].update({
    field: Capability.RELIABLE_CURRENT
    for field in ("prices.open", "prices.high", "prices.low", "prices.close", "prices.adjusted", "prices.volume")
})
PROVIDER_CAPABILITIES["YFinanceProvider"].update({
    field: Capability.PARTIAL
    for field in (
        "prices.dividends", "prices.splits", "fundamentals.revenue", "fundamentals.eps",
        "fundamentals.net_income", "fundamentals.ebitda", "fundamentals.margins",
        "fundamentals.roe", "fundamentals.cash", "fundamentals.debt",
        "fundamentals.shares_outstanding", "valuation.market_cap",
        "valuation.enterprise_value", "valuation.pe", "valuation.ev_ebitda",
        "valuation.price_sales", "classification.sector", "classification.industry",
        "classification.description", "classification.activities", "shareholder.dividends",
        "shareholder.buybacks",
    )
})
PROVIDER_CAPABILITIES["SECCompanyFactsProvider"].update({
    field: Capability.RELIABLE_POINT_IN_TIME
    for field in (
        "fundamentals.revenue", "fundamentals.eps", "fundamentals.net_income",
        "fundamentals.margins", "fundamentals.cash", "fundamentals.debt",
        "shareholder.dividends", "shareholder.buybacks",
    )
})
PROVIDER_CAPABILITIES["AlphaLabEstimateSnapshots"].update({
    field: Capability.RELIABLE_POINT_IN_TIME
    for field in (
        "estimates.fiscal_period", "estimates.eps", "estimates.revenue",
        "estimates.analyst_count", "estimates.timestamp", "estimates.dispersion",
    )
})


FIELD_SOURCE_PRIORITY: dict[str, tuple[str, ...]] = {
    "universe.exchange": ("NasdaqTraderUniverseProvider", "YFinanceProvider"),
    "universe.security_type": ("NasdaqTraderUniverseProvider", "YFinanceProvider"),
    "prices.ohlcv": ("YFinanceProvider",),
    "fundamentals.reported": ("SECCompanyFactsProvider", "YFinanceProvider"),
    "business_classification": ("YFinanceProvider", "SECCompanyFactsProvider"),
    "estimates.history": ("AlphaLabEstimateSnapshots",),
    "documents.filings": ("SECCompanyFactsProvider",),
}


def capability(provider: str, field: str) -> Capability:
    """Unknown capability always fails closed to UNSUPPORTED."""
    return PROVIDER_CAPABILITIES.get(provider, {}).get(field, Capability.UNSUPPORTED)


def provider_capability_matrix() -> dict[str, dict[str, Capability]]:
    """Return a complete matrix; omitted claims are explicitly UNSUPPORTED."""
    return {
        provider: {field: capability(provider, field) for field in CAPABILITY_FIELDS}
        for provider in PROVIDER_CAPABILITIES
    }


def preferred_provider(field: str, available: set[str]) -> str | None:
    """Select by documented field-level priority, never ingestion order."""
    return next(
        (provider for provider in FIELD_SOURCE_PRIORITY.get(field, ()) if provider in available),
        None,
    )
