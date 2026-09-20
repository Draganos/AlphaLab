"""Persistence/orchestration for the AlphaLab News Engine (Phase 2C).

Deliberately separate from `alpha_lab.research`/`.screener`/`.strategy`/
`.backtest`/`.portfolio`/`.ratings`/`.factors`: nothing in any of those
modules imports from here, and this module never touches `StockResearch`,
`LiveResearchRecord`, `composite_score`, or ranking -- see this project's
established evidence-layer boundary (Donatien, Macro Regime, Alignment,
CalibrationAlignment all follow the same rule).

This is an evidence layer only: ingestion -> validated/stored article ->
deterministic read exposure. No sentiment, no relevance score, no
NewsImpact classification -- those remain explicitly future work.

Coverage caveat (stated here because it governs `refresh`'s design):
`YFinanceProvider.get_news` returns whatever Yahoo currently has cached
for a ticker -- a handful of recent items, not a historical archive. A
refresh can only ever capture news *from the point it is run onward*; it
can never retroactively backfill news that existed before AlphaLab first
refreshed a given ticker. This mirrors `alpha_lab.macro.regime`'s
"market-derived proxies, never official data" honesty pattern: the
limitation is documented, not glossed over.

Read methods here are pure database reads -- safe to call from a
Streamlit render. `refresh` calls a provider (via
`ProviderErrorKind`-classified failures) and is meant to be triggered
explicitly (a script or a UI button), never from an ordinary page render.
"""

from datetime import UTC, date, datetime, time

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alpha_lab.database.models import NewsArticleRecord
from alpha_lab.news.article import NewsArticle, parse_news_article
from alpha_lab.providers.errors import ProviderError
from alpha_lab.providers.interfaces import ResearchNewsProvider

# Conservative chunk size for get_history_for_tickers' IN (...) clause --
# well under even pre-3.32.0 SQLite's default SQLITE_MAX_VARIABLE_NUMBER
# (999), which some system-linked Python installs still have regardless
# of this project's own dev environment.
_TICKER_CHUNK_SIZE = 500


class NewsRefreshResult:
    """Observable outcome of one `NewsService.refresh` call -- explicit
    counts, never a silent success/failure."""

    def __init__(self, *, ticker: str, fetched: int, stored: int, duplicates: int, invalid: int):
        self.ticker = ticker
        self.fetched = fetched
        self.stored = stored
        self.duplicates = duplicates
        self.invalid = invalid


class NewsService:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- reads: pure DB, no network, no computation -------------------------

    def get_history(
        self,
        ticker: str,
        *,
        as_of: date | None = None,
        since: date | None = None,
        until: date | None = None,
        limit: int | None = None,
    ) -> list[NewsArticleRecord]:
        """Point-in-time-safe read. `as_of`, when given, filters
        `retrieved_at <= end_of(as_of)` -- the sole PIT eligibility rule
        (mirrors `ExternalCalibrationService.get_calibration_as_of`'s
        `retrieved_at`-based filtering exactly, for the same reason: an
        article's own `published_at` is the source's claim, not proof
        AlphaLab actually had it stored by `as_of`). Omitting `as_of`
        returns every article ever retrieved (the full current view).
        `since`/`until` are a display/windowing convenience filtered on
        `published_at`, not a PIT mechanism.
        """
        with Session(self.engine) as session:
            statement = select(NewsArticleRecord).where(NewsArticleRecord.ticker == ticker)
            if as_of is not None:
                upper_bound = datetime.combine(as_of, time.max)
                statement = statement.where(NewsArticleRecord.retrieved_at <= upper_bound)
            if since is not None:
                statement = statement.where(NewsArticleRecord.published_at >= datetime.combine(since, time.min))
            if until is not None:
                statement = statement.where(NewsArticleRecord.published_at <= datetime.combine(until, time.max))
            statement = statement.order_by(
                NewsArticleRecord.published_at.desc(), NewsArticleRecord.content_hash.desc()
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = session.scalars(statement).all()
            session.expunge_all()
            return list(rows)

    def get_history_for_tickers(
        self, tickers: list[str], *, as_of: date | None = None
    ) -> dict[str, list[NewsArticleRecord]]:
        """Same point-in-time-safe filtering as `get_history`, for many
        tickers in one query instead of one call per ticker -- for a
        caller building something universe-wide (e.g. the Evidence
        Coverage page's Universe Breakdown), calling `get_history` in a
        per-ticker loop costs one full round-trip per ticker for no
        reason; a single `ticker IN (...)` query costs the same either
        way regardless of how large the universe is. Does not support
        `since`/`until`/`limit` -- no current caller needs them here;
        add them if a future one does. A ticker with no articles is
        simply absent from the returned dict, never an empty-list-vs-
        missing-key ambiguity to disambiguate -- use `.get(ticker, [])`.

        Chunks the `IN (...)` clause at `_TICKER_CHUNK_SIZE` tickers per
        query rather than one query for the whole list -- some SQLite
        builds (pre-3.32.0's default `SQLITE_MAX_VARIABLE_NUMBER=999`;
        still the case for some system-linked Python installs even though
        this project's own dev environment allows far more) reject a
        query with more bind parameters than that limit, and this is
        specifically meant to be called with the whole configured
        universe -- exactly the scenario the limit would otherwise bite.
        """
        if not tickers:
            return {}
        normalized = list(tickers)
        rows = []
        with Session(self.engine) as session:
            for start in range(0, len(normalized), _TICKER_CHUNK_SIZE):
                chunk = normalized[start : start + _TICKER_CHUNK_SIZE]
                statement = select(NewsArticleRecord).where(NewsArticleRecord.ticker.in_(chunk))
                if as_of is not None:
                    upper_bound = datetime.combine(as_of, time.max)
                    statement = statement.where(NewsArticleRecord.retrieved_at <= upper_bound)
                statement = statement.order_by(
                    NewsArticleRecord.published_at.desc(), NewsArticleRecord.content_hash.desc()
                )
                rows.extend(session.scalars(statement).all())
            session.expunge_all()
        history_by_ticker: dict[str, list[NewsArticleRecord]] = {}
        for row in rows:
            history_by_ticker.setdefault(row.ticker, []).append(row)
        return history_by_ticker

    # --- refresh: explicit, provider call happens before any DB write ------

    def refresh(self, provider: ResearchNewsProvider, ticker: str) -> NewsRefreshResult:
        """Fetch -> validate -> hash-dedup -> write. The provider call
        happens entirely before any database write (fetch-before-write,
        the same pattern used by every other evidence layer here), so a
        failed refresh always leaves previously stored articles completely
        untouched -- there is nothing to overwrite, since this table is
        append-only.

        One malformed article never aborts the whole refresh -- it is
        counted as `invalid` and skipped, mirroring Macro Regime's
        "one ticker failing doesn't abort the others" rule applied at the
        article level instead of the ticker level.
        """
        raw_items = provider.get_news(ticker)  # may raise ProviderError; nothing written yet
        retrieved_at = datetime.now(UTC).replace(tzinfo=None)
        provider_name = getattr(provider, "provider_name", type(provider).__name__)

        parsed: list[NewsArticle] = []
        invalid = 0
        for raw in raw_items:
            article = parse_news_article(raw, ticker=ticker, provider=provider_name)
            if article is None:
                invalid += 1
            else:
                parsed.append(article)

        stored = 0
        duplicates = 0
        with Session(self.engine) as session:
            for article in parsed:
                existing = session.scalar(
                    select(NewsArticleRecord).where(NewsArticleRecord.content_hash == article.content_hash)
                )
                if existing is not None:
                    duplicates += 1
                    continue
                # A savepoint per article, not a rollback of the whole
                # session: a duplicate-key race on one article must never
                # discard articles already flushed earlier in this same
                # refresh loop.
                try:
                    with session.begin_nested():
                        session.add(NewsArticleRecord(
                            ticker=article.ticker,
                            content_hash=article.content_hash,
                            title=article.title,
                            publisher=article.publisher,
                            url=article.url,
                            summary=article.summary,
                            published_at=article.published_at,
                            retrieved_at=retrieved_at,
                            provider=article.provider,
                            raw_payload=article.raw_payload,
                        ))
                except IntegrityError:
                    # A concurrent refresh stored the same article first;
                    # the unique constraint on content_hash is the actual
                    # idempotency guarantee.
                    duplicates += 1
                    continue
                stored += 1
            session.commit()

        return NewsRefreshResult(
            ticker=ticker, fetched=len(raw_items), stored=stored, duplicates=duplicates, invalid=invalid
        )
