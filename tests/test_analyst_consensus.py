"""Deterministic, offline tests for Analyst Consensus: rating computation,
missing-category safety, target/upside calculation, and provenance."""

from datetime import date

import pandas as pd
import pytest

from alpha_lab.providers.yfinance_provider import YFinanceProvider
from alpha_lab.research.analyst_consensus import (
    AnalystRating,
    build_analyst_consensus,
    compute_rating_score,
    compute_upside_to_mean,
    map_score_to_rating,
)


def test_complete_recommendation_counts_produce_a_rating():
    consensus = build_analyst_consensus(
        ticker="NVDA", strong_buy=18, buy=12, hold=5, sell=1, strong_sell=0,
        target_current=100.0, target_low=80.0, target_mean=130.0,
        target_median=125.0, target_high=160.0, as_of=date.today(), source="YFinanceProvider",
    )
    assert consensus.rating == AnalystRating.BUY
    assert consensus.total_analysts == 36
    assert consensus.coverage == 1.0


def test_all_strong_buy_maps_to_strong_buy():
    score = compute_rating_score(strong_buy=10, buy=0, hold=0, sell=0, strong_sell=0)
    assert score == 2.0
    assert map_score_to_rating(score) == AnalystRating.STRONG_BUY


def test_all_strong_sell_maps_to_strong_sell():
    score = compute_rating_score(strong_buy=0, buy=0, hold=0, sell=0, strong_sell=10)
    assert score == -2.0
    assert map_score_to_rating(score) == AnalystRating.STRONG_SELL


def test_balanced_hold_maps_to_neutral():
    score = compute_rating_score(strong_buy=0, buy=0, hold=10, sell=0, strong_sell=0)
    assert score == 0.0
    assert map_score_to_rating(score) == AnalystRating.NEUTRAL


def test_missing_category_never_silently_becomes_zero():
    """The exact scenario from the brief: strong_buy=10, buy=5, and the
    other three categories missing (not zero) must never be interpreted as
    10/5/0/0/0 -- that would fabricate a consensus the data doesn't support."""
    score = compute_rating_score(strong_buy=10, buy=5, hold=None, sell=None, strong_sell=None)
    assert score is None
    assert map_score_to_rating(score) == AnalystRating.REVIEW


def test_missing_category_result_is_review_not_a_fabricated_rating():
    consensus = build_analyst_consensus(
        ticker="XYZ", strong_buy=10, buy=5, hold=None, sell=None, strong_sell=None,
        target_current=None, target_low=None, target_mean=None, target_median=None,
        target_high=None, as_of=date.today(), source="YFinanceProvider",
    )
    assert consensus.rating == AnalystRating.REVIEW
    assert consensus.total_analysts is None
    assert consensus.rating_score is None


def test_confirmed_zero_analysts_is_review_not_neutral():
    """All five categories present and all zero: a confirmed absence of
    coverage, not a legitimate 'neutral' opinion."""
    consensus = build_analyst_consensus(
        ticker="THIN", strong_buy=0, buy=0, hold=0, sell=0, strong_sell=0,
        target_current=None, target_low=None, target_mean=None, target_median=None,
        target_high=None, as_of=date.today(), source="YFinanceProvider",
    )
    assert consensus.rating == AnalystRating.REVIEW
    assert consensus.total_analysts == 0


def test_zero_analysts_across_the_board_is_a_valid_zero_not_missing():
    """Distinguish confirmed-zero (all categories present, summing to zero)
    from missing (some categories absent) -- total_analysts is 0, not None,
    when every category was actually reported."""
    consensus = build_analyst_consensus(
        ticker="THIN", strong_buy=0, buy=0, hold=0, sell=0, strong_sell=0,
        target_current=None, target_low=None, target_mean=None, target_median=None,
        target_high=None, as_of=date.today(), source="YFinanceProvider",
    )
    assert consensus.total_analysts == 0


@pytest.mark.parametrize(
    "target_mean,target_current,expected",
    [
        (130.0, 100.0, pytest.approx(0.30)),
        (90.0, 100.0, pytest.approx(-0.10)),
        (100.0, 100.0, pytest.approx(0.0)),
    ],
)
def test_valid_upside_calculation(target_mean, target_current, expected):
    assert compute_upside_to_mean(target_mean, target_current) == expected


def test_upside_is_none_when_current_price_is_zero():
    """Division by a zero denominator must never happen -- unavailable, not
    an exception or an infinite value."""
    assert compute_upside_to_mean(130.0, 0.0) is None


def test_upside_is_none_when_either_value_missing():
    assert compute_upside_to_mean(None, 100.0) is None
    assert compute_upside_to_mean(130.0, None) is None


def test_upside_is_none_for_non_finite_inputs():
    assert compute_upside_to_mean(float("inf"), 100.0) is None
    assert compute_upside_to_mean(130.0, float("nan")) is None


def test_missing_targets_do_not_fabricate_current_price():
    consensus = build_analyst_consensus(
        ticker="NVDA", strong_buy=5, buy=5, hold=5, sell=0, strong_sell=0,
        target_current=None, target_low=None, target_mean=None, target_median=None,
        target_high=None, as_of=date.today(), source="YFinanceProvider",
    )
    assert consensus.target_current is None
    assert consensus.upside_to_mean is None
    # Coverage still reflects the 9 documented fields (5 counts, 4 targets):
    # only the 5 counts are present here.
    assert consensus.coverage == pytest.approx(5 / 9)


def test_provenance_is_recorded():
    consensus = build_analyst_consensus(
        ticker="NVDA", strong_buy=18, buy=12, hold=5, sell=1, strong_sell=0,
        target_current=100.0, target_low=80.0, target_mean=130.0,
        target_median=125.0, target_high=160.0, as_of=date(2026, 1, 1), source="YFinanceProvider",
    )
    assert consensus.source == "YFinanceProvider"
    assert consensus.as_of == date(2026, 1, 1)
    assert consensus.source_version


# --- provider raw-data extraction (offline, mocked Ticker) -----------------


class _FakeTicker:
    def __init__(self, recommendations=None, targets=None):
        self._recommendations = recommendations if recommendations is not None else pd.DataFrame()
        self._targets = targets or {}

    def get_recommendations(self):
        return self._recommendations

    def get_analyst_price_targets(self):
        return self._targets


def test_provider_extracts_only_the_current_month_row(monkeypatch):
    frame = pd.DataFrame(
        [
            {"period": "0m", "strongBuy": 18, "buy": 12, "hold": 5, "sell": 1, "strongSell": 0},
            {"period": "-1m", "strongBuy": 99, "buy": 99, "hold": 99, "sell": 99, "strongSell": 99},
        ]
    )
    provider = YFinanceProvider()
    monkeypatch.setattr(
        provider, "_ticker", lambda symbol: _FakeTicker(frame, {"current": 100.0, "mean": 130.0})
    )
    raw = provider.get_analyst_consensus("NVDA")
    assert raw["strong_buy"] == 18
    assert raw["buy"] == 12


def test_provider_returns_none_for_categories_missing_from_the_current_row(monkeypatch):
    frame = pd.DataFrame([{"period": "0m", "strongBuy": 10, "buy": 5}])
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: _FakeTicker(frame, {}))
    raw = provider.get_analyst_consensus("XYZ")
    assert raw["strong_buy"] == 10
    assert raw["buy"] == 5
    assert raw["hold"] is None
    assert raw["sell"] is None
    assert raw["strong_sell"] is None


def test_provider_returns_empty_when_no_current_month_row_exists(monkeypatch):
    frame = pd.DataFrame([{"period": "-1m", "strongBuy": 10, "buy": 5, "hold": 1, "sell": 0, "strongSell": 0}])
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: _FakeTicker(frame, {}))
    raw = provider.get_analyst_consensus("XYZ")
    assert raw["strong_buy"] is None


def test_provider_returns_empty_for_a_completely_empty_recommendations_frame(monkeypatch):
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: _FakeTicker(pd.DataFrame(), {}))
    raw = provider.get_analyst_consensus("NOCOVERAGE")
    assert raw["strong_buy"] is None
    assert raw["target_current"] is None


def test_provider_error_from_recommendations_propagates_classified(monkeypatch):
    import yfinance.exceptions as yf_exceptions
    from alpha_lab.providers.errors import ProviderError, ProviderErrorKind

    class _RaisingTicker(_FakeTicker):
        def get_recommendations(self):
            raise yf_exceptions.YFRateLimitError()

    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: _RaisingTicker())
    with pytest.raises(ProviderError) as excinfo:
        provider.get_analyst_consensus("NVDA")
    assert excinfo.value.kind == ProviderErrorKind.RATE_LIMITED
