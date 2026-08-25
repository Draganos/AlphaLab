"""Independent, auditable Phase 1 quantitative factor calculations."""

import numpy as np
import pandas as pd


def _safe_growth(current: float, prior: float) -> float:
    return np.nan if pd.isna(current) or pd.isna(prior) or prior == 0 else current / abs(prior) - (1 if prior > 0 else -1)


def calculate_factors(prices: pd.Series, fundamentals: pd.DataFrame) -> dict[str, float]:
    """Return raw factors using only supplied observations, oldest to newest."""
    close = prices.dropna().astype(float).sort_index()
    fund = fundamentals.sort_values("period").copy() if "period" in fundamentals else fundamentals.copy()
    result: dict[str, float] = {}
    result["last_price"] = close.iloc[-1] if not close.empty else np.nan
    for days, name in [(21, "return_1m"), (63, "return_3m"), (126, "return_6m"), (252, "return_12m")]:
        result[name] = close.iloc[-1] / close.iloc[-days - 1] - 1 if len(close) > days else np.nan
    result["momentum_12_1"] = close.iloc[-22] / close.iloc[-253] - 1 if len(close) > 252 else np.nan
    for window in (50, 200):
        ma = close.tail(window).mean() if len(close) >= window else np.nan
        result[f"distance_ma{window}"] = close.iloc[-1] / ma - 1 if pd.notna(ma) and ma else np.nan
    returns = close.pct_change().dropna()
    result["volatility"] = returns.tail(252).std() * np.sqrt(252) if len(returns) >= 2 else np.nan
    result["max_drawdown"] = (close / close.cummax() - 1).min() if not close.empty else np.nan
    if not fund.empty:
        latest = fund.iloc[-1]
        prior_yoy = fund.iloc[-5] if len(fund) >= 5 else None
        prior = fund.iloc[-2] if len(fund) >= 2 else None
        result["eps_yoy_growth"] = _safe_growth(latest.get("eps"), prior_yoy.get("eps")) if prior_yoy is not None else np.nan
        result["revenue_yoy_growth"] = _safe_growth(latest.get("revenue"), prior_yoy.get("revenue")) if prior_yoy is not None else np.nan
        result["revenue_sequential_growth"] = _safe_growth(latest.get("revenue"), prior.get("revenue")) if prior is not None else np.nan
        revenue = latest.get("revenue")
        for numerator, name in [(latest.get("ebitda"), "ebitda_margin"), (latest.get("net_income"), "net_margin"), (latest.get("free_cash_flow"), "fcf_margin")]:
            result[name] = numerator / revenue if pd.notna(numerator) and pd.notna(revenue) and revenue else np.nan
        equity = latest.get("total_equity")
        result["roe"] = latest.get("net_income") / equity if pd.notna(latest.get("net_income")) and pd.notna(equity) and equity else np.nan
        debt, cash, ebitda = latest.get("total_debt"), latest.get("cash"), latest.get("ebitda")
        result["net_debt"] = debt - cash if pd.notna(debt) and pd.notna(cash) else np.nan
        result["debt_to_ebitda"] = debt / ebitda if pd.notna(debt) and pd.notna(ebitda) and ebitda > 0 else np.nan
    return result


def percentile_scores(raw: pd.DataFrame, lower_is_better: set[str] | None = None) -> pd.DataFrame:
    """Cross-sectional 0–100 scores; unavailable observations stay unavailable."""
    lower_is_better = lower_is_better or {"volatility", "debt_to_ebitda"}
    scored = pd.DataFrame(index=raw.index)
    for column in raw:
        ascending = column not in lower_is_better
        scored[column] = raw[column].rank(pct=True, ascending=ascending, na_option="keep") * 100
    return scored
