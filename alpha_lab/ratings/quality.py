"""Nullable quality, financial-strength, and shareholder-return ratios."""

import math
from typing import Any


def calculate_quality_factors(
    current: dict[str, Any], prior: dict[str, Any] | None = None
) -> dict[str, float | None]:
    prior = prior or {}
    revenue = _positive(current.get("revenue"))
    equity = _positive(current.get("total_equity"))
    assets = _positive(current.get("total_assets"))
    debt = _number(current.get("total_debt"))
    ebitda = _positive(current.get("ebitda"))
    ebit = _number(current.get("ebit"))
    interest = _positive(current.get("interest_expense"))
    fcf = _number(current.get("free_cash_flow"))
    net_income = _number(current.get("net_income"))
    dividends = _number(current.get("dividends_paid"))
    buybacks = _number(current.get("share_repurchases"))
    market_cap = _positive(current.get("market_cap"))
    return {
        "gross_margin": _ratio(current.get("gross_profit"), revenue),
        "ebitda_margin": _ratio(current.get("ebitda"), revenue),
        "operating_margin": _ratio(ebit, revenue),
        "net_margin": _ratio(net_income, revenue),
        "fcf_margin": _ratio(fcf, revenue),
        "roe": _ratio(net_income, equity),
        "roa": _ratio(net_income, assets),
        "fcf_conversion": _ratio(fcf, net_income)
        if net_income and net_income > 0
        else None,
        "net_debt": debt - current.get("cash", 0)
        if debt is not None and _number(current.get("cash")) is not None
        else None,
        "debt_ebitda": _ratio(debt, ebitda),
        "debt_equity": _ratio(debt, equity),
        "current_ratio": _ratio(
            current.get("current_assets"), _positive(current.get("current_liabilities"))
        ),
        "interest_coverage": _ratio(ebit, interest),
        "cash_flow_to_debt": _ratio(fcf, _positive(debt)),
        "dividend_yield": _ratio(abs(dividends), market_cap)
        if dividends is not None
        else None,
        "buyback_yield": _ratio(abs(buybacks), market_cap)
        if buybacks is not None
        else None,
        "total_shareholder_yield": _ratio(
            abs(dividends + buybacks), market_cap
        )
        if market_cap and dividends is not None and buybacks is not None
        else None,
        "revenue_growth": _growth(revenue, prior.get("revenue")),
        "eps_growth": _growth(current.get("eps"), prior.get("eps")),
    }


def _growth(current: Any, prior: Any) -> float | None:
    current_number, prior_number = _number(current), _number(prior)
    if current_number is None or prior_number is None or prior_number == 0:
        return None
    return current_number / abs(prior_number) - (1 if prior_number > 0 else -1)


def _ratio(numerator: Any, denominator: Any) -> float | None:
    first, second = _number(numerator), _number(denominator)
    return None if first is None or second is None or second <= 0 else first / second


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None
