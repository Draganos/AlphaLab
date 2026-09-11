"""Offline, deterministic tests for alpha_lab.news.article's pure parsing/
validation/hashing. No network, no database."""

from datetime import datetime

from alpha_lab.news.article import compute_content_hash, parse_news_article

_VALID_RAW = {
    "title": "Exxon beats earnings estimates",
    "url": "https://example.com/article-1",
    "publisher": "Reuters",
    "summary": "EPS beat consensus.",
    "published_at": datetime(2024, 6, 1, 9, 0),
    "raw": {"id": "abc123"},
}


def test_parses_a_well_formed_article():
    article = parse_news_article(_VALID_RAW, ticker="XOM", provider="FakeProvider")
    assert article is not None
    assert article.ticker == "XOM"
    assert article.title == "Exxon beats earnings estimates"
    assert article.url == "https://example.com/article-1"
    assert article.publisher == "Reuters"
    assert article.summary == "EPS beat consensus."
    assert article.published_at == datetime(2024, 6, 1, 9, 0)
    assert article.provider == "FakeProvider"
    assert article.raw_payload == {"id": "abc123"}


def test_missing_title_is_invalid():
    raw = dict(_VALID_RAW, title=None)
    assert parse_news_article(raw, ticker="XOM", provider="p") is None


def test_empty_title_is_invalid():
    raw = dict(_VALID_RAW, title="   ")
    assert parse_news_article(raw, ticker="XOM", provider="p") is None


def test_missing_url_is_invalid():
    raw = dict(_VALID_RAW, url=None)
    assert parse_news_article(raw, ticker="XOM", provider="p") is None


def test_malformed_url_is_invalid():
    raw = dict(_VALID_RAW, url="not-a-url")
    assert parse_news_article(raw, ticker="XOM", provider="p") is None


def test_non_http_url_is_invalid():
    raw = dict(_VALID_RAW, url="ftp://example.com/x")
    assert parse_news_article(raw, ticker="XOM", provider="p") is None


def test_missing_published_at_is_invalid():
    raw = dict(_VALID_RAW, published_at=None)
    assert parse_news_article(raw, ticker="XOM", provider="p") is None


def test_missing_publisher_and_summary_degrade_to_none_not_fabricated():
    raw = dict(_VALID_RAW, publisher=None, summary=None)
    article = parse_news_article(raw, ticker="XOM", provider="p")
    assert article is not None
    assert article.publisher is None
    assert article.summary is None


def test_content_hash_is_deterministic():
    first = compute_content_hash(ticker="XOM", url="https://x/1", title="T", published_at=datetime(2024, 6, 1))
    second = compute_content_hash(ticker="XOM", url="https://x/1", title="T", published_at=datetime(2024, 6, 1))
    assert first == second


def test_content_hash_changes_with_url_title_or_published_at():
    base = compute_content_hash(ticker="XOM", url="https://x/1", title="T", published_at=datetime(2024, 6, 1))
    assert base != compute_content_hash(ticker="XOM", url="https://x/2", title="T", published_at=datetime(2024, 6, 1))
    assert base != compute_content_hash(ticker="XOM", url="https://x/1", title="Other", published_at=datetime(2024, 6, 1))
    assert base != compute_content_hash(ticker="XOM", url="https://x/1", title="T", published_at=datetime(2024, 6, 2))


def test_content_hash_is_unaffected_by_publisher_or_summary_changes():
    """A provider correcting a typo in publisher/summary must not be
    treated as a new article -- only ticker/url/title/published_at
    determine identity."""
    first = parse_news_article(dict(_VALID_RAW, publisher="Reuters"), ticker="XOM", provider="p")
    second = parse_news_article(dict(_VALID_RAW, publisher="Reuters Wire"), ticker="XOM", provider="p")
    assert first.content_hash == second.content_hash
