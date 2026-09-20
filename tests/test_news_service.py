"""Offline, deterministic tests for NewsService: refresh orchestration,
hash-dedup, per-article failure resilience, and point-in-time-safe reads.
No network access -- uses a fake in-memory ResearchNewsProvider."""

from datetime import UTC, date, datetime

import pytest

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import NewsArticleRecord, Security
from alpha_lab.news.service import NewsService
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
from sqlalchemy.orm import Session


class _FakeNewsProvider:
    provider_name = "FakeNewsProvider"

    def __init__(self, items=None, *, raises=None):
        self._items = items or []
        self._raises = raises

    def get_news(self, ticker, since=None):
        if self._raises is not None:
            raise self._raises
        return self._items


def _article(title="T", url="https://x/1", published_at=datetime(2024, 6, 1, 9, 0), **overrides):
    record = {"title": title, "url": url, "publisher": "Reuters", "summary": "S", "published_at": published_at, "raw": {}}
    record.update(overrides)
    return record


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="XOM", country="US", currency="USD"))
        session.commit()
    try:
        yield engine
    finally:
        engine.dispose()


# --- refresh: dedup, invalid handling, idempotency --------------------------


def test_refresh_stores_new_valid_articles(engine):
    service = NewsService(engine)
    result = service.refresh(_FakeNewsProvider([_article()]), "XOM")
    assert result.fetched == 1
    assert result.stored == 1
    assert result.duplicates == 0
    assert result.invalid == 0
    assert len(service.get_history("XOM")) == 1


def test_refresh_skips_malformed_articles_without_aborting_the_batch(engine):
    items = [_article(title="Valid"), _article(title=None, url="also-bad")]
    result = NewsService(engine).refresh(_FakeNewsProvider(items), "XOM")
    assert result.fetched == 2
    assert result.stored == 1
    assert result.invalid == 1


def test_refresh_is_idempotent_for_identical_articles(engine):
    service = NewsService(engine)
    service.refresh(_FakeNewsProvider([_article()]), "XOM")
    result = service.refresh(_FakeNewsProvider([_article()]), "XOM")
    assert result.stored == 0
    assert result.duplicates == 1
    assert len(service.get_history("XOM")) == 1


def test_refresh_with_multiple_new_articles_stores_all_of_them(engine):
    items = [_article(title="A", url="https://x/a"), _article(title="B", url="https://x/b"), _article(title="C", url="https://x/c")]
    result = NewsService(engine).refresh(_FakeNewsProvider(items), "XOM")
    assert result.stored == 3


def test_provider_error_raises_and_writes_nothing(engine):
    service = NewsService(engine)
    with pytest.raises(ProviderError):
        service.refresh(_FakeNewsProvider(raises=ProviderError(ProviderErrorKind.NETWORK_UNAVAILABLE, "Fake", "down")), "XOM")
    assert service.get_history("XOM") == []


def test_a_later_failed_refresh_never_erases_previously_stored_articles(engine):
    """The core failure-preservation invariant, applied to News: once an
    article is stored, a later failed refresh for the same ticker leaves
    it (and any other previously stored articles) completely untouched."""
    service = NewsService(engine)
    service.refresh(_FakeNewsProvider([_article()]), "XOM")
    with pytest.raises(ProviderError):
        service.refresh(_FakeNewsProvider(raises=ProviderError(ProviderErrorKind.RATE_LIMITED, "Fake", "429")), "XOM")
    assert len(service.get_history("XOM")) == 1


def test_get_history_is_empty_before_any_refresh(engine):
    assert NewsService(engine).get_history("XOM") == []


# --- get_history: PIT safety, ordering, windowing ---------------------------


def _insert_article(engine, *, content_hash, published_at, retrieved_at, ticker="XOM"):
    with Session(engine) as session:
        session.add(NewsArticleRecord(
            ticker=ticker, content_hash=content_hash, title=f"Article {content_hash}",
            publisher="P", url=f"https://x/{content_hash}", summary=None,
            published_at=published_at, retrieved_at=retrieved_at, provider="test", raw_payload={},
        ))
        session.commit()


def test_as_of_excludes_an_article_retrieved_after_the_query_date(engine):
    """Point-in-time regression: an article whose published_at predates a
    historical as_of, but whose retrieved_at is AFTER it, must never leak
    into that historical query -- retrieved_at is the sole eligibility
    boundary, never published_at."""
    _insert_article(engine, content_hash="early", published_at=datetime(2024, 3, 1), retrieved_at=datetime(2024, 3, 1, 10, 0))
    _insert_article(engine, content_hash="late", published_at=datetime(2024, 3, 1), retrieved_at=datetime(2024, 3, 5, 10, 0))

    as_of_before_second_retrieval = NewsService(engine).get_history("XOM", as_of=date(2024, 3, 3))
    assert [row.content_hash for row in as_of_before_second_retrieval] == ["early"]

    as_of_after_second_retrieval = NewsService(engine).get_history("XOM", as_of=date(2024, 3, 5))
    assert {row.content_hash for row in as_of_after_second_retrieval} == {"early", "late"}


def test_omitting_as_of_returns_every_stored_article(engine):
    _insert_article(engine, content_hash="a", published_at=datetime(2024, 1, 1), retrieved_at=datetime(2024, 1, 1))
    _insert_article(engine, content_hash="b", published_at=datetime(2024, 6, 1), retrieved_at=datetime(2026, 9, 1))
    assert len(NewsService(engine).get_history("XOM")) == 2


def test_get_history_orders_by_published_at_descending(engine):
    _insert_article(engine, content_hash="old", published_at=datetime(2024, 1, 1), retrieved_at=datetime(2024, 1, 1))
    _insert_article(engine, content_hash="new", published_at=datetime(2024, 6, 1), retrieved_at=datetime(2024, 6, 1))
    history = NewsService(engine).get_history("XOM")
    assert [row.content_hash for row in history] == ["new", "old"]


def test_get_history_since_until_filter_on_published_at(engine):
    _insert_article(engine, content_hash="jan", published_at=datetime(2024, 1, 15), retrieved_at=datetime(2024, 1, 15))
    _insert_article(engine, content_hash="jun", published_at=datetime(2024, 6, 15), retrieved_at=datetime(2024, 6, 15))
    result = NewsService(engine).get_history("XOM", since=date(2024, 5, 1), until=date(2024, 12, 31))
    assert [row.content_hash for row in result] == ["jun"]


def test_get_history_is_scoped_to_the_requested_ticker(engine):
    with Session(engine) as session:
        session.add(Security(ticker="AAPL", country="US", currency="USD"))
        session.commit()
    _insert_article(engine, content_hash="xom1", published_at=datetime(2024, 1, 1), retrieved_at=datetime(2024, 1, 1), ticker="XOM")
    _insert_article(engine, content_hash="aapl1", published_at=datetime(2024, 1, 1), retrieved_at=datetime(2024, 1, 1), ticker="AAPL")
    assert [row.content_hash for row in NewsService(engine).get_history("XOM")] == ["xom1"]


def test_get_history_respects_limit(engine):
    for i in range(5):
        _insert_article(engine, content_hash=f"a{i}", published_at=datetime(2024, 1, i + 1), retrieved_at=datetime(2024, 1, i + 1))
    assert len(NewsService(engine).get_history("XOM", limit=2)) == 2


def test_get_history_is_deterministic_across_repeated_calls(engine):
    service = NewsService(engine)
    service.refresh(_FakeNewsProvider([_article(title="A", url="https://x/a"), _article(title="B", url="https://x/b")]), "XOM")
    first = [row.content_hash for row in service.get_history("XOM")]
    second = [row.content_hash for row in service.get_history("XOM")]
    assert first == second


# --- get_history_for_tickers: batched read, same PIT/ordering guarantees ---


def test_get_history_for_tickers_matches_per_ticker_get_history(engine):
    """The whole point of the batched read: identical results to calling
    get_history once per ticker, for a universe-wide caller that would
    otherwise pay one query per ticker (see app/dashboard/pages/
    8_Evidence_Coverage.py's Universe Breakdown, which used to do that)."""
    with Session(engine) as session:
        session.add(Security(ticker="CVX", country="US", currency="USD"))
        session.commit()
    _insert_article(engine, content_hash="xom-old", published_at=datetime(2024, 1, 1), retrieved_at=datetime(2024, 1, 1), ticker="XOM")
    _insert_article(engine, content_hash="xom-new", published_at=datetime(2024, 2, 1), retrieved_at=datetime(2024, 2, 1), ticker="XOM")
    _insert_article(engine, content_hash="cvx-only", published_at=datetime(2024, 1, 15), retrieved_at=datetime(2024, 1, 15), ticker="CVX")

    service = NewsService(engine)
    batched = service.get_history_for_tickers(["XOM", "CVX"])

    assert [row.content_hash for row in batched["XOM"]] == [row.content_hash for row in service.get_history("XOM")]
    assert [row.content_hash for row in batched["CVX"]] == [row.content_hash for row in service.get_history("CVX")]


def test_get_history_for_tickers_omits_tickers_with_no_articles(engine):
    with Session(engine) as session:
        session.add(Security(ticker="CVX", country="US", currency="USD"))
        session.commit()
    _insert_article(engine, content_hash="xom-only", published_at=datetime(2024, 1, 1), retrieved_at=datetime(2024, 1, 1), ticker="XOM")

    batched = NewsService(engine).get_history_for_tickers(["XOM", "CVX"])

    assert "XOM" in batched
    assert "CVX" not in batched
    assert batched.get("CVX", []) == []


def test_get_history_for_tickers_respects_as_of(engine):
    """Same PIT-safety guarantee as get_history's own as_of -- retrieved_at
    is the sole eligibility boundary, applied identically in the batched
    query."""
    _insert_article(engine, content_hash="early", published_at=datetime(2024, 3, 1), retrieved_at=datetime(2024, 3, 1))
    _insert_article(engine, content_hash="late", published_at=datetime(2024, 3, 1), retrieved_at=datetime(2024, 3, 5))

    as_of_before = NewsService(engine).get_history_for_tickers(["XOM"], as_of=date(2024, 3, 3))
    assert [row.content_hash for row in as_of_before["XOM"]] == ["early"]


def test_get_history_for_tickers_with_empty_ticker_list_returns_empty_dict(engine):
    assert NewsService(engine).get_history_for_tickers([]) == {}


def test_get_history_for_tickers_chunks_beyond_the_single_query_limit(engine):
    """Regression test for a self-review finding: get_history_for_tickers
    used to build one IN (...) clause with a bind parameter per ticker,
    which some SQLite builds (pre-3.32.0's default
    SQLITE_MAX_VARIABLE_NUMBER=999) reject once the ticker count is large
    -- exactly the scale (thousands of tickers) the Universe Breakdown page
    calls this with. Seeds tickers spanning multiple _TICKER_CHUNK_SIZE
    (500) chunks, with articles on both sides of at least one chunk
    boundary, and confirms every ticker's history still comes back complete
    and correctly ordered -- chunking must never drop or misorder results."""
    from alpha_lab.news.service import _TICKER_CHUNK_SIZE

    ticker_count = _TICKER_CHUNK_SIZE + 250  # spans three chunks
    tickers = [f"T{i:05d}" for i in range(ticker_count)]
    with Session(engine) as session:
        for ticker in tickers:
            session.add(Security(ticker=ticker, country="US", currency="USD"))
        session.commit()

    # One article each for a ticker in the first chunk, right at the first
    # chunk boundary, and in the last (partial) chunk.
    boundary_tickers = [tickers[0], tickers[_TICKER_CHUNK_SIZE - 1], tickers[_TICKER_CHUNK_SIZE], tickers[-1]]
    for ticker in boundary_tickers:
        _insert_article(
            engine, content_hash=f"{ticker}-a", published_at=datetime(2024, 1, 1),
            retrieved_at=datetime(2024, 1, 1), ticker=ticker,
        )

    service = NewsService(engine)
    batched = service.get_history_for_tickers(tickers)

    assert set(batched.keys()) == set(boundary_tickers)
    for ticker in boundary_tickers:
        assert [row.content_hash for row in batched[ticker]] == [row.content_hash for row in service.get_history(ticker)]
