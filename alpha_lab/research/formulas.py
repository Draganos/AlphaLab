"""Best-effort, hand-verified provenance metadata for live rating metrics.

These dictionaries describe *how* a metric is derived, matching the actual
calculations in ``alpha_lab.ratings.quality``, ``alpha_lab.ratings.valuation``,
``alpha_lab.ratings.estimates``, and ``alpha_lab.factors.engine``. They are
deliberately kept as plain text/keys rather than re-executed formulas: adding
a metric here documents it for :mod:`alpha_lab.research.build` without
duplicating (and risking drift from) the calculation code itself.
"""

# Metrics that are read directly from a provider rather than computed from
# other stored fields. Everything else is treated as calculated.
DIRECTLY_SOURCED_METRICS = {
    "current_consensus_eps",
    "current_consensus_revenue",
    "analyst_count",
    "estimate_dispersion",
}

FORMULAS: dict[str, str] = {
    "pe": "Price / Diluted EPS",
    "forward_pe": "Price / Forward Consensus EPS",
    "price_sales": "Market Cap / Revenue",
    "ev_ebitda": "Enterprise Value / EBITDA",
    "ev_sales": "Enterprise Value / Revenue",
    "price_fcf": "Market Cap / Free Cash Flow",
    "fcf_yield": "Free Cash Flow / Market Cap",
    "earnings_yield": "EPS / Price",
    "gross_margin": "Gross Profit / Revenue",
    "ebitda_margin": "EBITDA / Revenue",
    "operating_margin": "EBIT / Revenue",
    "net_margin": "Net Income / Revenue",
    "fcf_margin": "Free Cash Flow / Revenue",
    "roe": "Net Income / Total Equity",
    "roa": "Net Income / Total Assets",
    "fcf_conversion": "Free Cash Flow / Net Income",
    "net_debt": "Total Debt - Cash",
    "debt_ebitda": "Total Debt / EBITDA",
    "debt_equity": "Total Debt / Total Equity",
    "current_ratio": "Current Assets / Current Liabilities",
    "interest_coverage": "EBIT / Interest Expense",
    "cash_flow_to_debt": "Free Cash Flow / Total Debt",
    "dividend_yield": "abs(Dividends Paid) / Market Cap",
    "buyback_yield": "abs(Share Repurchases) / Market Cap",
    "total_shareholder_yield": "abs(Dividends Paid + Share Repurchases) / Market Cap",
    "revenue_growth": "(Current Revenue / abs(Prior Revenue)) - 1",
    "eps_growth": "(Current EPS / abs(Prior EPS)) - 1",
    "eps_revision_7d": "Current Consensus EPS / Consensus EPS as of 7 Days Prior - 1",
    "eps_revision_30d": "Current Consensus EPS / Consensus EPS as of 30 Days Prior - 1",
    "eps_revision_90d": "Current Consensus EPS / Consensus EPS as of 90 Days Prior - 1",
    "revenue_revision_7d": "Current Consensus Revenue / Consensus Revenue as of 7 Days Prior - 1",
    "revenue_revision_30d": "Current Consensus Revenue / Consensus Revenue as of 30 Days Prior - 1",
    "revenue_revision_90d": "Current Consensus Revenue / Consensus Revenue as of 90 Days Prior - 1",
    "return_1m": "Trailing 1-month price return",
    "return_3m": "Trailing 3-month price return",
    "return_6m": "Trailing 6-month price return",
    "return_12m": "Trailing 12-month price return",
    "momentum_12_1": "Trailing 12-month return excluding the most recent month",
    "distance_ma50": "(Price - 50-day Moving Average) / 50-day Moving Average",
    "distance_ma200": "(Price - 200-day Moving Average) / 200-day Moving Average",
}

# Metric -> other raw metrics (already present in the live raw-metric table)
# that participate in its calculation. This is intentionally best-effort:
# several inputs (e.g. raw revenue, EPS) are consumed upstream in
# alpha_lab.ratings and are not themselves exposed as live raw metrics, so
# they cannot be listed here without changing those modules.
KNOWN_INPUT_METRICS: dict[str, tuple[str, ...]] = {
    "price_sales": ("market_cap",),
    "ev_ebitda": ("market_cap", "enterprise_value"),
    "ev_sales": ("market_cap", "enterprise_value"),
    "price_fcf": ("market_cap",),
    "fcf_yield": ("market_cap",),
    "dividend_yield": ("market_cap",),
    "buyback_yield": ("market_cap",),
    "total_shareholder_yield": ("market_cap",),
    "debt_ebitda": ("net_debt",),
    "cash_flow_to_debt": ("net_debt",),
}

_RATIO_METRICS = {
    "gross_margin", "ebitda_margin", "operating_margin", "net_margin", "fcf_margin",
    "roe", "roa", "fcf_conversion", "dividend_yield", "buyback_yield",
    "total_shareholder_yield", "revenue_growth", "eps_growth", "fcf_yield",
    "earnings_yield", "eps_revision_7d", "eps_revision_30d", "eps_revision_90d",
    "revenue_revision_7d", "revenue_revision_30d", "revenue_revision_90d",
    "return_1m", "return_3m", "return_6m", "return_12m", "momentum_12_1",
    "distance_ma50", "distance_ma200", "debt_ebitda", "debt_equity",
    "cash_flow_to_debt", "current_ratio", "interest_coverage", "estimate_dispersion",
}
_MULTIPLE_METRICS = {"pe", "forward_pe", "price_sales", "ev_ebitda", "ev_sales", "price_fcf"}
_COUNT_METRICS = {"analyst_count", "upward_revisions", "downward_revisions"}
_CURRENCY_METRICS = {
    "market_cap", "enterprise_value", "net_debt", "current_consensus_eps",
    "current_consensus_revenue",
}


def metric_unit(name: str) -> str | None:
    if name in _RATIO_METRICS:
        return "ratio"
    if name in _MULTIPLE_METRICS:
        return "multiple"
    if name in _COUNT_METRICS:
        return "count"
    if name in _CURRENCY_METRICS:
        return "currency"
    return None
