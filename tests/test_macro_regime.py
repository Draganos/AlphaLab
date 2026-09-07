"""Offline, deterministic tests for alpha_lab.macro.regime's pure
computation. No network access."""

from datetime import date, timedelta

import pandas as pd
import pytest

from alpha_lab.macro.regime import MacroRegime, build_macro_assessment


def _flat_frame(value: float, days: int = 60, end: date = date(2024, 6, 1)) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(end), periods=days)
    return pd.DataFrame({"close": [value] * days}, index=idx)


def _trending_frame(start_value: float, end_value: float, days: int = 60, end: date = date(2024, 6, 1)) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(end), periods=days)
    step = (end_value - start_value) / (days - 1)
    return pd.DataFrame({"close": [start_value + step * i for i in range(days)]}, index=idx)


_CALM_MARKET = {
    "^VIX": _flat_frame(12.0),
    "^TNX": _flat_frame(45.0),
    "^IRX": _flat_frame(40.0),  # spread = +0.5 -> risk-on
    "DX-Y.NYB": _flat_frame(100.0),
    "CL=F": _flat_frame(70.0),
    "GC=F": _flat_frame(2000.0),
}


def test_full_coverage_calm_market_is_risk_on():
    assessment = build_macro_assessment(scope="US", price_histories=_CALM_MARKET, as_of=date(2024, 6, 1))
    assert assessment.regime == MacroRegime.RISK_ON
    assert assessment.regime_score == pytest.approx(1.0)
    assert assessment.coverage == 1.0
    assert assessment.confidence == 1.0


def test_high_vix_and_inverted_curve_is_risk_off():
    histories = {
        "^VIX": _flat_frame(30.0),
        "^TNX": _flat_frame(40.0),
        "^IRX": _flat_frame(50.0),  # spread = -1.0 -> inverted
    }
    assessment = build_macro_assessment(scope="US", price_histories=histories, as_of=date(2024, 6, 1))
    assert assessment.regime == MacroRegime.RISK_OFF
    assert assessment.regime_score == pytest.approx(-1.0)


def test_mixed_signals_produce_neutral():
    histories = {
        "^VIX": _flat_frame(30.0),   # risk-off signal
        "^TNX": _flat_frame(45.0),
        "^IRX": _flat_frame(40.0),   # spread = +0.5 -> risk-on signal
    }
    assessment = build_macro_assessment(scope="US", price_histories=histories, as_of=date(2024, 6, 1))
    assert assessment.regime == MacroRegime.NEUTRAL
    assert assessment.regime_score == pytest.approx(0.0)


def test_missing_vix_still_produces_a_regime_from_yield_curve_alone():
    histories = dict(_CALM_MARKET)
    del histories["^VIX"]
    assessment = build_macro_assessment(scope="US", price_histories=histories, as_of=date(2024, 6, 1))
    assert assessment.regime == MacroRegime.RISK_ON  # yield curve alone is available and positive
    assert assessment.coverage == pytest.approx(0.8)  # 4 of 5 indicators available
    vix = next(i for i in assessment.indicators if i.ticker == "^VIX")
    assert vix.value is None
    assert vix.signal is None


def test_missing_one_yield_curve_leg_makes_the_whole_spread_unavailable():
    """The spread cannot be partially computed -- if either TNX or IRX is
    missing, the derived indicator is unavailable, not approximated."""
    histories = dict(_CALM_MARKET)
    del histories["^IRX"]
    assessment = build_macro_assessment(scope="US", price_histories=histories, as_of=date(2024, 6, 1))
    spread = next(i for i in assessment.indicators if i.name == "10Y-3M Treasury Yield Spread")
    assert spread.value is None
    assert spread.signal is None
    # VIX alone is still available and calm -> regime is still assessable.
    assert assessment.regime == MacroRegime.RISK_ON


def test_no_regime_relevant_data_at_all_is_review_not_neutral():
    histories = {"DX-Y.NYB": _flat_frame(100.0)}
    assessment = build_macro_assessment(scope="US", price_histories=histories, as_of=date(2024, 6, 1))
    assert assessment.regime == MacroRegime.REVIEW
    assert assessment.regime_score is None
    assert assessment.coverage == pytest.approx(0.2)  # 1 of 5 indicators
    assert assessment.confidence < assessment.coverage  # halved for REVIEW


def test_completely_empty_input_is_review_with_zero_coverage():
    assessment = build_macro_assessment(scope="US", price_histories={}, as_of=date(2024, 6, 1))
    assert assessment.regime == MacroRegime.REVIEW
    assert assessment.coverage == 0.0
    assert assessment.confidence == 0.0
    for indicator in assessment.indicators:
        assert indicator.value is None
        assert indicator.signal is None


def test_usd_oil_gold_trend_signals_are_informational_not_regime_inputs():
    """A strongly trending USD/oil/gold must not move the regime score --
    only VIX and the yield curve spread may."""
    histories = dict(_CALM_MARKET)
    histories["DX-Y.NYB"] = _trending_frame(90.0, 110.0)
    histories["CL=F"] = _trending_frame(50.0, 90.0)
    histories["GC=F"] = _trending_frame(1800.0, 2200.0)
    assessment = build_macro_assessment(scope="US", price_histories=histories, as_of=date(2024, 6, 1))
    assert assessment.regime == MacroRegime.RISK_ON
    assert assessment.regime_score == pytest.approx(1.0)  # unchanged by the trends above
    usd = next(i for i in assessment.indicators if i.ticker == "DX-Y.NYB")
    assert usd.signal == 1  # rising trend correctly detected as its own informational signal


def test_short_price_history_is_insufficient_for_a_trend_signal():
    histories = dict(_CALM_MARKET)
    histories["CL=F"] = _flat_frame(70.0, days=10)  # fewer than the 50-day window
    assessment = build_macro_assessment(scope="US", price_histories=histories, as_of=date(2024, 6, 1))
    oil = next(i for i in assessment.indicators if i.ticker == "CL=F")
    assert oil.value is not None  # latest price is still known
    assert oil.signal is None     # but not enough history for a trend read


def test_methodology_version_is_stamped():
    assessment = build_macro_assessment(scope="US", price_histories=_CALM_MARKET, as_of=date(2024, 6, 1))
    assert assessment.methodology_version == "macro-regime-v1"
