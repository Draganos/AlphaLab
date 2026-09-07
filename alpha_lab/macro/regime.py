"""AlphaLab Macro Regime: a deterministic, market-derived regime read.

Scope decision (approved before implementation): built entirely from
already-ingested `Price` history for a small fixed set of market-observable
proxy tickers via yfinance/`IngestionService` -- no new provider, no FRED,
no official economic-data integration. This means every indicator here is a
**market-derived proxy**, never official economic data (CPI, GDP,
employment) -- that distinction is preserved in every label and docstring
below and must never be blurred in any UI/report that reads this module.

Architectural boundary: exactly like Donatien, this is informational only.
Nothing here is imported by `alpha_lab.research`/`alpha_lab.screener`/
`alpha_lab.strategy`/`alpha_lab.backtest`/`alpha_lab.portfolio`, and this
module never touches the fundamental score, Analyst Consensus, Technical
Summary, or AI Research Rating. See `MacroAssessment`'s docstring.

Modularity/degradation requirement (approved before implementation): each
proxy has its own availability status. A missing instrument reduces
`coverage` and is reported as UNAVAILABLE for that one indicator -- it never
fabricates a value, never forces a Neutral/0 reading, and never invalidates
the other indicators. `regime` itself becomes `MacroRegime.REVIEW` only when
none of the two textbook risk indicators (see below) are available at all.

Regime classification uses only the two indicators with a well-established,
textbook risk-on/risk-off interpretation:
- CBOE Volatility Index (VIX) level -- low VIX = calm/risk-on, high VIX =
  fear/risk-off. This is standard market convention, not an AlphaLab
  invention.
- 10-Year minus 3-Month Treasury yield spread -- a curve inversion
  (negative spread) is the textbook recession/risk-off signal (the same
  spread the NY Fed's own recession-probability model uses).

USD strength, oil, and gold are reported as separate informational
indicators (price vs. its own trailing moving average, the same signal
convention `alpha_lab.research.technical` already uses) and contribute to
`coverage`/`confidence`, but are deliberately NOT folded into the regime
score -- their relationship to "risk regime" is not a single
well-established direction, and forcing one here would be inventing an
interpretation the market itself doesn't agree on.
"""

from datetime import date
from enum import StrEnum
import math

import pandas as pd
from pydantic import BaseModel, Field

MACRO_METHODOLOGY_VERSION = "macro-regime-v1"

# ticker -> (label, category). All are market-observable proxies fetched
# and stored exactly like any other AlphaLab security (Security/Price
# tables) via the existing IngestionService -- no new provider architecture.
MACRO_PROXY_TICKERS: dict[str, tuple[str, str]] = {
    "^VIX": ("CBOE Volatility Index", "VOLATILITY"),
    "^TNX": ("10-Year Treasury Note Yield (x10)", "RATES"),
    "^IRX": ("13-Week Treasury Bill Yield (x10)", "RATES"),
    "DX-Y.NYB": ("US Dollar Index", "CURRENCY"),
    "CL=F": ("WTI Crude Oil Futures", "COMMODITY"),
    "GC=F": ("Gold Futures", "COMMODITY"),
}

TOTAL_INDICATOR_COUNT = 5  # VIX, yield curve spread, USD, oil, gold


class MacroIndicatorCategory(StrEnum):
    VOLATILITY = "VOLATILITY"
    RATES = "RATES"
    CURRENCY = "CURRENCY"
    COMMODITY = "COMMODITY"


class MacroRegime(StrEnum):
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"
    # Neither VIX nor the yield curve spread was available -- never forced
    # to NEUTRAL, which would misrepresent "no basis to judge" as "judged
    # and balanced".
    REVIEW = "REVIEW"


class MacroIndicator(BaseModel):
    name: str
    category: MacroIndicatorCategory
    ticker: str
    value: float | None
    signal: int | None = Field(None, ge=-1, le=1)
    as_of: date | None
    source: str


class MacroAssessment(BaseModel):
    """A separate research output, not a scoring input. See module
    docstring -- nothing in alpha_lab.research/screener/strategy/backtest/
    portfolio reads this. `regime`/`regime_score` are derived only from the
    VOLATILITY and RATES-spread indicators; USD/oil/gold are informational.
    """

    scope: str
    regime: MacroRegime
    regime_score: float | None = Field(None, ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    coverage: float = Field(ge=0, le=1)
    indicators: list[MacroIndicator]
    as_of: date
    source: str
    methodology_version: str = MACRO_METHODOLOGY_VERSION


# --- versioned, documented thresholds ---------------------------------

_VIX_THRESHOLDS_V1: tuple[tuple[float, int], ...] = (
    # (upper bound exclusive, signal) evaluated low-to-high
    (15.0, 1),   # calm -> risk-on
    (25.0, 0),   # normal -> neutral
)
_VIX_HIGH_SIGNAL = -1  # >= 25 -> elevated fear -> risk-off

# 10Y-3M spread, in percentage points (Yahoo quotes ^TNX/^IRX x10).
_YIELD_CURVE_THRESHOLDS_V1: tuple[tuple[float, int], ...] = (
    (-0.1, -1),  # inverted -> recession-risk signal -> risk-off
    (0.1, 0),    # flat -> neutral
)
_YIELD_CURVE_HIGH_SIGNAL = 1  # > 0.1 -> normal/steep -> risk-on

_REGIME_SCORE_THRESHOLDS_V1: tuple[tuple[float, MacroRegime], ...] = (
    (0.5, MacroRegime.RISK_ON),
    (-0.5, MacroRegime.NEUTRAL),
)
_REGIME_SCORE_FLOOR = MacroRegime.RISK_OFF


def _band(value: float, thresholds: tuple[tuple[float, int], ...], high: int) -> int:
    for upper, signal in thresholds:
        if value < upper:
            return signal
    return high


def _map_score_to_regime(score: float | None) -> MacroRegime:
    if score is None:
        return MacroRegime.REVIEW
    for threshold, regime in _REGIME_SCORE_THRESHOLDS_V1:
        if score >= threshold:
            return regime
    return _REGIME_SCORE_FLOOR


def _latest_valid(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = series.iloc[-1]
    if value is None or pd.isna(value):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _latest_date(frame: pd.DataFrame) -> date | None:
    if frame.empty:
        return None
    return pd.Timestamp(frame.index[-1]).date()


def _trend_signal(closes: pd.Series, window: int = 50) -> int | None:
    """Same convention as alpha_lab.research.technical's moving-average
    indicators: latest close above its trailing SMA -> +1, below -> -1,
    equal -> 0. None if there isn't enough history."""
    if len(closes) < window:
        return None
    sma = closes.rolling(window=window, min_periods=window).mean()
    latest_close = _latest_valid(closes)
    latest_sma = _latest_valid(sma)
    if latest_close is None or latest_sma is None:
        return None
    if latest_close > latest_sma:
        return 1
    if latest_close < latest_sma:
        return -1
    return 0


def build_macro_assessment(
    *,
    scope: str,
    price_histories: dict[str, pd.DataFrame],
    as_of: date,
    source: str = "AlphaLabPriceHistory",
) -> MacroAssessment:
    """Pure, deterministic computation from already-stored price history --
    no network call. `price_histories` maps ticker -> OHLC frame (ascending
    by date, at least a `close` column), exactly the shape
    `alpha_lab.database.models.Price` rows already take for any ingested
    ticker -- the same contract `alpha_lab.research.technical.build_technical_summary`
    uses for a single security's price history.
    """

    def _closes(ticker: str) -> pd.Series:
        frame = price_histories.get(ticker)
        if frame is None or frame.empty or "close" not in frame:
            return pd.Series(dtype=float)
        return frame["close"].astype(float)

    indicators: list[MacroIndicator] = []

    # --- VIX (level-based regime indicator) ---
    vix_closes = _closes("^VIX")
    vix_value = _latest_valid(vix_closes)
    vix_signal = None if vix_value is None else _band(vix_value, _VIX_THRESHOLDS_V1, _VIX_HIGH_SIGNAL)
    indicators.append(MacroIndicator(
        name="CBOE Volatility Index (VIX)", category=MacroIndicatorCategory.VOLATILITY,
        ticker="^VIX", value=vix_value, signal=vix_signal,
        as_of=_latest_date(price_histories.get("^VIX", pd.DataFrame())), source=source,
    ))

    # --- Yield curve spread: 10Y - 3M, in percentage points (regime indicator) ---
    tnx_value = _latest_valid(_closes("^TNX"))
    irx_value = _latest_valid(_closes("^IRX"))
    if tnx_value is not None and irx_value is not None:
        spread_value = (tnx_value - irx_value) / 10.0
        spread_signal = _band(spread_value, _YIELD_CURVE_THRESHOLDS_V1, _YIELD_CURVE_HIGH_SIGNAL)
        spread_as_of = _latest_date(price_histories.get("^TNX", pd.DataFrame()))
    else:
        spread_value = None
        spread_signal = None
        spread_as_of = None
    indicators.append(MacroIndicator(
        name="10Y-3M Treasury Yield Spread", category=MacroIndicatorCategory.RATES,
        ticker="^TNX-^IRX", value=spread_value, signal=spread_signal,
        as_of=spread_as_of, source=source,
    ))

    # --- USD / Oil / Gold: informational trend indicators (not regime inputs) ---
    for name, ticker, category in (
        ("US Dollar Index (DXY) trend", "DX-Y.NYB", MacroIndicatorCategory.CURRENCY),
        ("WTI Crude Oil trend", "CL=F", MacroIndicatorCategory.COMMODITY),
        ("Gold trend", "GC=F", MacroIndicatorCategory.COMMODITY),
    ):
        closes = _closes(ticker)
        indicators.append(MacroIndicator(
            name=name, category=category, ticker=ticker,
            value=_latest_valid(closes), signal=_trend_signal(closes),
            as_of=_latest_date(price_histories.get(ticker, pd.DataFrame())), source=source,
        ))

    available = sum(1 for indicator in indicators if indicator.value is not None)
    coverage = available / TOTAL_INDICATOR_COUNT

    regime_signals = [signal for signal in (vix_signal, spread_signal) if signal is not None]
    regime_score = sum(regime_signals) / len(regime_signals) if regime_signals else None
    regime = _map_score_to_regime(regime_score)

    confidence = coverage if regime != MacroRegime.REVIEW else coverage * 0.5

    return MacroAssessment(
        scope=scope, regime=regime, regime_score=regime_score,
        confidence=round(confidence, 3), coverage=round(coverage, 3),
        indicators=indicators, as_of=as_of, source=source,
    )
