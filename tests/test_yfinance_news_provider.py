"""Offline, deterministic tests for YFinanceProvider.get_news and its
defensive field-extraction across the two known yfinance news JSON shapes.
No network access."""

import pytest

from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from alpha_lab.providers.yfinance_provider import YFinanceProvider, _normalize_news_item


class _FakeTicker:
    def __init__(self, items=None, *, raises=None):
        self._items = items or []
        self._raises = raises

    def get_news(self, count=10, tab="news"):
        if self._raises is not None:
            raise self._raises
        return self._items


# --- _normalize_news_item: both known shapes -------------------------------


def test_normalizes_the_legacy_flat_shape():
    item = {
        "uuid": "u1", "title": "T1", "publisher": "Reuters",
        "link": "https://example.com/1", "providerPublishTime": 1717228800,
        "summary": "S1",
    }
    record = _normalize_news_item(item)
    assert record is not None
    assert record["title"] == "T1"
    assert record["url"] == "https://example.com/1"
    assert record["publisher"] == "Reuters"
    assert record["summary"] == "S1"
    assert record["published_at"] is not None
    assert record["raw"] == item


def test_normalizes_the_nested_content_shape():
    item = {
        "content": {
            "title": "T2",
            "provider": {"displayName": "Bloomberg"},
            "canonicalUrl": {"url": "https://example.com/2"},
            "pubDate": "2024-06-01T12:00:00Z",
            "summary": "S2",
        }
    }
    record = _normalize_news_item(item)
    assert record is not None
    assert record["title"] == "T2"
    assert record["url"] == "https://example.com/2"
    assert record["publisher"] == "Bloomberg"
    assert record["published_at"] is not None


def test_nested_shape_falls_back_to_click_through_url_when_canonical_is_absent():
    item = {"content": {"title": "T3", "clickThroughUrl": {"url": "https://example.com/3"}, "pubDate": "2024-06-01T12:00:00Z"}}
    record = _normalize_news_item(item)
    assert record["url"] == "https://example.com/3"


def test_unrecognized_shape_returns_none_not_a_guess():
    assert _normalize_news_item({"nonsense": True}) is None
    assert _normalize_news_item({}) is None


def test_non_dict_item_returns_none():
    assert _normalize_news_item("not a dict") is None
    assert _normalize_news_item(None) is None


def test_missing_required_field_in_either_shape_returns_none():
    # title present but no url at all
    assert _normalize_news_item({"title": "T", "providerPublishTime": 1717228800}) is None
    # url present but no timestamp
    assert _normalize_news_item({"title": "T", "link": "https://x/1"}) is None


# --- YFinanceProvider.get_news: end to end ----------------------------------


def test_get_news_returns_normalized_records(monkeypatch):
    items = [
        {"title": "T1", "link": "https://x/1", "publisher": "Reuters", "providerPublishTime": 1717228800, "summary": "S1"},
        {"content": {"title": "T2", "canonicalUrl": {"url": "https://x/2"}, "pubDate": "2024-06-01T12:00:00Z"}},
    ]
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: _FakeTicker(items))
    records = provider.get_news("XOM")
    assert len(records) == 2
    assert {r["title"] for r in records} == {"T1", "T2"}


def test_get_news_silently_drops_malformed_items_rather_than_raising(monkeypatch):
    items = [
        {"title": "Valid", "link": "https://x/1", "providerPublishTime": 1717228800},
        {"nonsense": True},
        "not a dict",
    ]
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda symbol: _FakeTicker(items))
    records = provider.get_news("XOM")
    assert len(records) == 1
    assert records[0]["title"] == "Valid"


def test_get_news_returns_empty_list_for_no_news():
    provider = YFinanceProvider()
    provider._ticker = lambda symbol: _FakeTicker([])
    assert provider.get_news("NOCOVERAGE") == []


def test_get_news_propagates_classified_provider_error(monkeypatch):
    import yfinance.exceptions as yf_exceptions

    provider = YFinanceProvider()
    monkeypatch.setattr(
        provider, "_ticker", lambda symbol: _FakeTicker(raises=yf_exceptions.YFRateLimitError())
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.get_news("XOM")
    assert excinfo.value.kind == ProviderErrorKind.RATE_LIMITED
