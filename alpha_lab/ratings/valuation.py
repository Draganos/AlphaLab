"""Valuation ratios that preserve unavailable and invalid denominators."""

import math


def calculate_valuation_factors(
    *,
    price: float | None,
    shares: float | None,
    eps: float | None,
    forward_eps: float | None,
    revenue: float | None,
    ebitda: float | None,
    free_cash_flow: float | None,
    debt: float | None,
    cash: float | None,
) -> dict[str, float | None]:
    market_cap = price * shares if _positive(price) and _positive(shares) else None
    enterprise_value = (
        market_cap + debt - cash
        if market_cap is not None and _finite(debt) and _finite(cash)
        else None
    )
    pe = price / eps if _positive(price) and _positive(eps) else None
    forward_pe = (
        price / forward_eps if _positive(price) and _positive(forward_eps) else None
    )
    price_sales = (
        market_cap / revenue if market_cap is not None and _positive(revenue) else None
    )
    ev_ebitda = (
        enterprise_value / ebitda
        if enterprise_value is not None and _positive(ebitda)
        else None
    )
    ev_sales = (
        enterprise_value / revenue
        if enterprise_value is not None and _positive(revenue)
        else None
    )
    price_fcf = (
        market_cap / free_cash_flow
        if market_cap is not None and _positive(free_cash_flow)
        else None
    )
    return {
        "market_cap": market_cap,
        "enterprise_value": enterprise_value,
        "pe": pe,
        "forward_pe": forward_pe,
        "price_sales": price_sales,
        "ev_ebitda": ev_ebitda,
        "ev_sales": ev_sales,
        "price_fcf": price_fcf,
        "fcf_yield": free_cash_flow / market_cap
        if market_cap and _finite(free_cash_flow)
        else None,
        "earnings_yield": eps / price if _positive(eps) and _positive(price) else None,
    }


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _positive(value: float | None) -> bool:
    return _finite(value) and value > 0
