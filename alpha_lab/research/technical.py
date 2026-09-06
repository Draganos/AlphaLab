"""Technical / Indicators Summary: what AlphaLab's own stored price history says.

A separate research domain from AlphaLab's 0-100 fundamental score -- never
blended into it. Computed entirely from already-ingested OHLC price history
(no network calls; safe to compute during an explicit refresh or, since it
is a pure deterministic function of already-stored data, even at read time).

Methodology (``TECHNICAL_METHODOLOGY_VERSION``): each indicator maps its
latest reading to a signal in {-1 (Sell), 0 (Neutral), +1 (Buy)} using a
fixed, documented rule (see each ``_signal_*`` function). An indicator with
insufficient history or a non-finite/undefined reading contributes no
signal at all -- it is never coerced to Neutral, which would silently
understate conviction. ``moving_average_score``/``oscillator_score`` are the
mean of their group's *available* signals; ``overall_score`` is the mean of
whichever of those two group scores are available (TradingView's own
"Technical Rating" convention: the two groups are weighted equally as
blocks, not as one flat mean of every individual indicator). Scores map to
a rating via fixed, versioned thresholds. Below ``MIN_COVERAGE_THRESHOLD``
overall indicator coverage, the rating is forced to REVIEW rather than
displaying a rating computed from too little evidence.

Timeframe is explicit (``Timeframe.DAILY`` today) and carried on every
result so a future intraday timeframe is an additive enum value, not a
redesign -- see ``Timeframe``.
"""

from datetime import date
from enum import StrEnum
import math

import pandas as pd
from pydantic import BaseModel, Field

TECHNICAL_METHODOLOGY_VERSION = "technical-summary-v1"

# Conservative default: at least half of the 15 implemented indicators must
# be available before a rating is displayed. Documented, versioned, and
# overridable per call (e.g. for a deliberately smaller test universe) --
# never silently changed without a methodology version bump.
MIN_COVERAGE_THRESHOLD = 0.5

TOTAL_INDICATOR_COUNT = 15  # 8 moving averages + 7 oscillators; keep in sync below.


class Timeframe(StrEnum):
    DAILY = "DAILY"
    # Reserved for future phases -- adding a member here is additive, not a
    # redesign, as long as callers keep branching on Timeframe rather than
    # assuming DAILY.
    HOURLY_1 = "1H"
    HOURLY_4 = "4H"
    WEEKLY = "WEEKLY"


class TechnicalRating(StrEnum):
    STRONG_SELL = "STRONG_SELL"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"
    BUY = "BUY"
    STRONG_BUY = "STRONG_BUY"
    # Coverage below MIN_COVERAGE_THRESHOLD, or zero indicators available.
    REVIEW = "REVIEW"


class IndicatorCategory(StrEnum):
    MOVING_AVERAGE = "MOVING_AVERAGE"
    OSCILLATOR = "OSCILLATOR"


class IndicatorEvidence(BaseModel):
    name: str
    category: IndicatorCategory
    value: float | None
    signal: int | None = Field(None, ge=-1, le=1)
    timeframe: Timeframe
    as_of: date
    methodology_version: str = TECHNICAL_METHODOLOGY_VERSION


class TechnicalSummary(BaseModel):
    ticker: str
    overall_score: float | None = Field(None, ge=-1, le=1)
    overall_rating: TechnicalRating
    moving_average_score: float | None = Field(None, ge=-1, le=1)
    moving_average_rating: TechnicalRating
    oscillator_score: float | None = Field(None, ge=-1, le=1)
    oscillator_rating: TechnicalRating
    indicators: list[IndicatorEvidence]
    moving_average_available: int
    moving_average_total: int
    oscillator_available: int
    oscillator_total: int
    coverage: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    timeframe: Timeframe
    as_of: date
    source: str
    methodology_version: str = TECHNICAL_METHODOLOGY_VERSION


# --- versioned score->rating thresholds -----------------------------------

_RATING_THRESHOLDS_V1: tuple[tuple[float, TechnicalRating], ...] = (
    (0.5, TechnicalRating.STRONG_BUY),
    (0.1, TechnicalRating.BUY),
    (-0.1, TechnicalRating.NEUTRAL),
    (-0.5, TechnicalRating.SELL),
)
_RATING_FLOOR = TechnicalRating.STRONG_SELL


def _map_score_to_rating(score: float | None) -> TechnicalRating:
    if score is None:
        return TechnicalRating.REVIEW
    for threshold, rating in _RATING_THRESHOLDS_V1:
        if score >= threshold:
            return rating
    return _RATING_FLOOR


# --- indicator math (pure pandas, no network) -----------------------------


def _sma(closes: pd.Series, window: int) -> pd.Series:
    return closes.rolling(window=window, min_periods=window).mean()


def _ema(closes: pd.Series, span: int) -> pd.Series:
    return closes.ewm(span=span, adjust=False, min_periods=span).mean()


def _rsi(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return rsi


def _macd(closes: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series]:
    macd_line = _ema(closes, fast) - _ema(closes, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=slow + signal).mean()
    return macd_line, signal_line


def _stochastic_k(
    highs: pd.Series, lows: pd.Series, closes: pd.Series, k_period: int, k_smooth: int
) -> pd.Series:
    lowest_low = lows.rolling(k_period, min_periods=k_period).min()
    highest_high = highs.rolling(k_period, min_periods=k_period).max()
    span = highest_high - lowest_low
    raw_k = 100 * (closes - lowest_low) / span.replace(0, float("nan"))
    return raw_k.rolling(k_smooth, min_periods=k_smooth).mean()


def _cci(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int) -> pd.Series:
    typical = (highs + lows + closes) / 3
    sma_tp = typical.rolling(period, min_periods=period).mean()
    mean_deviation = typical.rolling(period, min_periods=period).apply(
        lambda window: (window - window.mean()).abs().mean(), raw=False
    )
    return (typical - sma_tp) / (0.015 * mean_deviation.replace(0, float("nan")))


def _williams_r(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int) -> pd.Series:
    highest_high = highs.rolling(period, min_periods=period).max()
    lowest_low = lows.rolling(period, min_periods=period).min()
    span = highest_high - lowest_low
    return -100 * (highest_high - closes) / span.replace(0, float("nan"))


def _adx(
    highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    up_move = highs.diff()
    down_move = -lows.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    prior_close = closes.shift()
    true_range = pd.concat(
        [highs - lows, (highs - prior_close).abs(), (lows - prior_close).abs()], axis=1
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, float("nan"))
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, float("nan"))
    di_sum = (plus_di + minus_di).replace(0, float("nan"))
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx, plus_di, minus_di


def _momentum(closes: pd.Series, period: int) -> pd.Series:
    return closes.diff(period)


def _latest_valid(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = series.iloc[-1]
    if value is None or pd.isna(value):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _evidence(
    name: str,
    category: IndicatorCategory,
    value: float | None,
    signal: int | None,
    *,
    as_of: date,
) -> IndicatorEvidence:
    return IndicatorEvidence(
        name=name,
        category=category,
        value=value,
        signal=signal,
        timeframe=Timeframe.DAILY,
        as_of=as_of,
    )


def _ma_indicator(name: str, ma_value: float | None, latest_close: float | None, as_of: date) -> IndicatorEvidence:
    """Signal rule for every moving average: price above -> Buy, below -> Sell, equal -> Neutral."""
    if ma_value is None or latest_close is None:
        return _evidence(name, IndicatorCategory.MOVING_AVERAGE, None, None, as_of=as_of)
    if latest_close > ma_value:
        signal = 1
    elif latest_close < ma_value:
        signal = -1
    else:
        signal = 0
    return _evidence(name, IndicatorCategory.MOVING_AVERAGE, ma_value, signal, as_of=as_of)


def _threshold_oscillator(
    name: str, value: float | None, *, buy_below: float, sell_above: float, as_of: date
) -> IndicatorEvidence:
    """Shared overbought/oversold signal rule for RSI, Stochastic %K, CCI,
    and Williams %R: below `buy_below` is oversold (Buy), above
    `sell_above` is overbought (Sell), between is Neutral."""
    if value is None:
        return _evidence(name, IndicatorCategory.OSCILLATOR, None, None, as_of=as_of)
    if value < buy_below:
        signal = 1
    elif value > sell_above:
        signal = -1
    else:
        signal = 0
    return _evidence(name, IndicatorCategory.OSCILLATOR, value, signal, as_of=as_of)


def build_technical_summary(
    ticker: str,
    price_history: pd.DataFrame,
    *,
    as_of: date,
    source: str,
    min_coverage: float = MIN_COVERAGE_THRESHOLD,
) -> TechnicalSummary:
    """Pure, deterministic computation from already-fetched daily OHLC data.

    `price_history` must be ascending by date with at least `close` (and
    `high`/`low` for oscillators needing them); this is exactly the shape
    AlphaLab already stores in the `Price` table / `get_price_history()`.
    Never calls a provider and never touches the database.
    """
    closes = price_history["close"].astype(float) if "close" in price_history else pd.Series(dtype=float)
    highs = price_history["high"].astype(float) if "high" in price_history else pd.Series(dtype=float)
    lows = price_history["low"].astype(float) if "low" in price_history else pd.Series(dtype=float)
    latest_close = _latest_valid(closes)

    indicators: list[IndicatorEvidence] = []

    for name, window in (("SMA10", 10), ("SMA20", 20), ("SMA50", 50), ("SMA100", 100), ("SMA200", 200)):
        value = _latest_valid(_sma(closes, window)) if len(closes) >= window else None
        indicators.append(_ma_indicator(name, value, latest_close, as_of))
    for name, span in (("EMA20", 20), ("EMA50", 50), ("EMA200", 200)):
        value = _latest_valid(_ema(closes, span)) if len(closes) >= span else None
        indicators.append(_ma_indicator(name, value, latest_close, as_of))

    rsi_value = _latest_valid(_rsi(closes, 14)) if len(closes) >= 15 else None
    indicators.append(_threshold_oscillator("RSI14", rsi_value, buy_below=30, sell_above=70, as_of=as_of))

    if len(closes) >= 35:
        macd_line, signal_line = _macd(closes, 12, 26, 9)
        macd_value = _latest_valid(macd_line)
        signal_value = _latest_valid(signal_line)
    else:
        macd_value = signal_value = None
    macd_signal = None
    if macd_value is not None and signal_value is not None:
        macd_signal = 1 if macd_value > signal_value else (-1 if macd_value < signal_value else 0)
    indicators.append(_evidence("MACD_12_26_9", IndicatorCategory.OSCILLATOR, macd_value, macd_signal, as_of=as_of))

    stoch_value = (
        _latest_valid(_stochastic_k(highs, lows, closes, 14, 3)) if len(closes) >= 20 else None
    )
    indicators.append(
        _threshold_oscillator("STOCH_14_3_3", stoch_value, buy_below=20, sell_above=80, as_of=as_of)
    )

    cci_value = _latest_valid(_cci(highs, lows, closes, 20)) if len(closes) >= 20 else None
    indicators.append(_threshold_oscillator("CCI20", cci_value, buy_below=-100, sell_above=100, as_of=as_of))

    if len(closes) >= 28:
        adx_value_series, plus_di, minus_di = _adx(highs, lows, closes, 14)
        adx_value = _latest_valid(adx_value_series)
        plus_di_value = _latest_valid(plus_di)
        minus_di_value = _latest_valid(minus_di)
    else:
        adx_value = plus_di_value = minus_di_value = None
    adx_signal = None
    if adx_value is not None and plus_di_value is not None and minus_di_value is not None:
        if adx_value > 25:
            adx_signal = 1 if plus_di_value > minus_di_value else (-1 if minus_di_value > plus_di_value else 0)
        else:
            adx_signal = 0
    indicators.append(_evidence("ADX14", IndicatorCategory.OSCILLATOR, adx_value, adx_signal, as_of=as_of))

    wr_value = _latest_valid(_williams_r(highs, lows, closes, 14)) if len(closes) >= 14 else None
    indicators.append(
        _threshold_oscillator("WILLIAMS_R14", wr_value, buy_below=-80, sell_above=-20, as_of=as_of)
    )

    momentum_value = _latest_valid(_momentum(closes, 10)) if len(closes) >= 11 else None
    momentum_signal = None
    if momentum_value is not None:
        momentum_signal = 1 if momentum_value > 0 else (-1 if momentum_value < 0 else 0)
    indicators.append(
        _evidence("MOMENTUM10", IndicatorCategory.OSCILLATOR, momentum_value, momentum_signal, as_of=as_of)
    )

    ma_indicators = [i for i in indicators if i.category == IndicatorCategory.MOVING_AVERAGE]
    osc_indicators = [i for i in indicators if i.category == IndicatorCategory.OSCILLATOR]
    ma_signals = [i.signal for i in ma_indicators if i.signal is not None]
    osc_signals = [i.signal for i in osc_indicators if i.signal is not None]

    ma_score = sum(ma_signals) / len(ma_signals) if ma_signals else None
    osc_score = sum(osc_signals) / len(osc_signals) if osc_signals else None
    group_scores = [score for score in (ma_score, osc_score) if score is not None]
    overall_score = sum(group_scores) / len(group_scores) if group_scores else None

    total_available = len(ma_signals) + len(osc_signals)
    coverage = total_available / TOTAL_INDICATOR_COUNT

    if coverage < min_coverage:
        overall_rating = TechnicalRating.REVIEW
    else:
        overall_rating = _map_score_to_rating(overall_score)
    ma_rating = _map_score_to_rating(ma_score) if len(ma_signals) / len(ma_indicators) >= min_coverage else TechnicalRating.REVIEW
    osc_rating = (
        _map_score_to_rating(osc_score)
        if osc_indicators and len(osc_signals) / len(osc_indicators) >= min_coverage
        else TechnicalRating.REVIEW
    )

    confidence = coverage if overall_rating != TechnicalRating.REVIEW else coverage * 0.5

    return TechnicalSummary(
        ticker=ticker.upper(),
        overall_score=overall_score,
        overall_rating=overall_rating,
        moving_average_score=ma_score,
        moving_average_rating=ma_rating,
        oscillator_score=osc_score,
        oscillator_rating=osc_rating,
        indicators=indicators,
        moving_average_available=len(ma_signals),
        moving_average_total=len(ma_indicators),
        oscillator_available=len(osc_signals),
        oscillator_total=len(osc_indicators),
        coverage=coverage,
        confidence=confidence,
        timeframe=Timeframe.DAILY,
        as_of=as_of,
        source=source,
    )
