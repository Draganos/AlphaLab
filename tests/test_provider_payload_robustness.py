"""Hostile-payload tests for YFinanceProvider's parsers, found by fuzzing a
fake yfinance `Ticker` (responses that are None, empty, NaN/inf, negative,
non-numeric or wrongly shaped). Contract under test: a bad cell becomes
None / is dropped and a missing response means "no data" -- never a
crash that aborts a whole ticker's refresh, and never a fabricated value."""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest
import yfinance

from alpha_lab.providers import YFinanceProvider

NAN = float("nan")


class _FakeTicker:
    cfg: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def _get(self, name):
        value = type(self).cfg.get(name)
        if isinstance(value, Exception):
            raise value
        return value

    def get_info(self): return self._get("info")
    def get_news(self, count=20): return self._get("news")
    def get_upgrades_downgrades(self): return self._get("upgrades")
    def get_recommendations(self): return self._get("recs")
    def get_analyst_price_targets(self): return self._get("targets")
    def history(self, **kwargs): return self._get("hist")

    @property
    def quarterly_income_stmt(self): return self._get("income")

    @property
    def quarterly_cashflow(self): return self._get("cash")

    @property
    def quarterly_balance_sheet(self): return self._get("bal")


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(yfinance, "Ticker", _FakeTicker)
    _FakeTicker.cfg = {}
    yield YFinanceProvider()
    _FakeTicker.cfg = {}


def _recs(**overrides):
    return pd.DataFrame([{"period": "0m", "strongBuy": 1, "buy": 2, "hold": 3, "sell": 0, "strongSell": 0, **overrides}])


# --- a missing (None) response means "no data", never a crash -----------------


def test_none_price_history_is_an_empty_frame(provider):
    _FakeTicker.cfg = {"hist": None}
    assert provider.get_price_history("X", date(2024, 1, 1), date(2024, 2, 1)).empty


def test_none_company_info_yields_empty_metadata_not_a_crash(provider):
    _FakeTicker.cfg = {"info": None}
    info = provider.get_company_info("x")
    assert info["ticker"] == "X" and info["company_name"] is None and info["market_cap"] is None


def test_none_financial_statements_are_an_empty_frame(provider):
    _FakeTicker.cfg = {"income": None, "cash": None, "bal": None}
    assert provider.get_financials("X").empty


def test_none_recommendations_and_targets_yield_none_fields(provider):
    _FakeTicker.cfg = {"recs": None, "targets": None}
    raw = provider.get_analyst_consensus("X")
    assert raw["strong_buy"] is None and raw["target_mean"] is None


# --- junk cells become None ----------------------------------------------------


@pytest.mark.parametrize("junk", [NAN, float("inf"), "many", -3, None])
def test_junk_recommendation_counts_become_none(provider, junk):
    _FakeTicker.cfg = {"recs": _recs(buy=junk), "targets": {}}
    assert provider.get_analyst_consensus("X")["buy"] is None


def test_clean_recommendation_counts_survive(provider):
    _FakeTicker.cfg = {"recs": _recs(), "targets": {"mean": 12.5}}
    raw = provider.get_analyst_consensus("X")
    assert (raw["strong_buy"], raw["buy"], raw["hold"]) == (1, 2, 3) and raw["target_mean"] == 12.5


def test_unusable_price_targets_become_none(provider):
    _FakeTicker.cfg = {"recs": _recs(), "targets": {"current": "x", "low": 0, "mean": -1, "median": NAN, "high": None}}
    raw = provider.get_analyst_consensus("X")
    assert all(raw[key] is None for key in ("target_current", "target_low", "target_mean", "target_median", "target_high"))


def test_non_string_company_info_values_are_text_or_none_never_nan(provider):
    _FakeTicker.cfg = {"info": {"longName": NAN, "exchange": 123, "quoteType": None, "marketCap": "1e9", "country": NAN}}
    info = provider.get_company_info("X")
    assert info["company_name"] is None and info["country"] is None
    assert info["exchange"] == "123" and info["market_cap"] == 1e9
    json.dumps(info, allow_nan=False)  # nothing NaN leaks into the stored record


# --- shape attacks that must already be (and stay) safe -----------------------


def test_malformed_news_items_are_dropped_and_good_ones_kept(provider):
    _FakeTicker.cfg = {"news": [
        None, "string", 123, [], {}, {"content": None}, {"content": "str"}, {"content": {"title": "t"}},
        {"content": {"title": "t", "canonicalUrl": "notadict", "pubDate": "garbage"}},
        {"title": "t", "link": "u", "providerPublishTime": 1.7e12},
        {"title": "t", "link": "u", "providerPublishTime": NAN},
        {"title": "t", "link": "u", "providerPublishTime": "1234"},
        {"title": "good", "link": "https://x", "providerPublishTime": 1700000000, "publisher": None},
    ]}
    items = provider.get_news("X")
    assert [item["title"] for item in items] == ["good"]


@pytest.mark.parametrize("value", [None, {"a": 1}, "oops"])
def test_non_list_news_is_empty(provider, value):
    _FakeTicker.cfg = {"news": value}
    assert provider.get_news("X") == []


def test_rating_changes_with_dirty_cells_keep_only_dated_rows_and_clean_numbers(provider):
    index = pd.to_datetime(["2024-01-02", pd.NaT, "2024-01-04"])
    frame = pd.DataFrame(
        {
            "Firm": ["A", None, NAN], "ToGrade": ["Buy", "Hold", None], "FromGrade": [None, "", "Sell"],
            "Action": ["up", "main", "init"], "priceTargetAction": ["Raises", "", "x"],
            "currentPriceTarget": [100.0, 0, NAN], "priorPriceTarget": [NAN, -5, "abc"],
        },
        index=index,
    )
    _FakeTicker.cfg = {"upgrades": frame}
    events = provider.get_analyst_rating_changes("X")
    assert len(events) == 2  # the NaT-dated row is dropped
    assert events[0]["current_price_target"] == 100.0 and events[0]["prior_price_target"] is None
    assert events[1]["firm"] is None and events[1]["current_price_target"] is None
    json.dumps(events, default=str, allow_nan=False)


def test_tz_aware_price_index_is_normalized_to_naive_dates(provider):
    index = pd.to_datetime(["2024-01-02"]).tz_localize("America/New_York")
    _FakeTicker.cfg = {"hist": pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Adj Close": [1.0], "Volume": [5]}, index=index
    )}
    frame = provider.get_price_history("X", date(2024, 1, 1), date(2024, 2, 1))
    assert frame.index.tz is None and frame.index[0].date() == date(2024, 1, 2)
    assert "adjusted_close" in frame.columns
