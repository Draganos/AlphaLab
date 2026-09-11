"""Pure, provider-independent News article model + validation.

Mirrors `alpha_lab.research.analyst_consensus.build_analyst_consensus`'s
split: the provider (`YFinanceProvider.get_news`) returns raw, already
best-effort-normalized dicts; parsing/validating those into the canonical
`NewsArticle` model is kept here, pure and provider-independent, so it can
be unit-tested without a provider at all.

`parse_news_article` never fabricates a missing field -- title, url, and
published_at are all required; a record missing any of them is invalid
(returns None) and must be skipped by the caller, never coerced into a
guessed value.
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
import hashlib
import json

from pydantic import BaseModel

NEWS_METHODOLOGY_VERSION = "news-engine-v1"


class NewsArticle(BaseModel):
    """One validated news article, exactly as the provider reported it --
    no sentiment, no relevance, no derived judgment of any kind."""

    ticker: str
    content_hash: str
    title: str
    publisher: str | None
    url: str
    summary: str | None
    published_at: datetime
    provider: str
    raw_payload: dict[str, Any]


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def compute_content_hash(*, ticker: str, url: str, title: str, published_at: datetime) -> str:
    """Deterministic article identity for deduplication. Deliberately
    excludes `summary`/`publisher` (a provider correcting a typo in either
    must not be treated as a "new" article) and excludes every
    AlphaLab-side timestamp (`retrieved_at`/`created_at`) -- refetching the
    same article on a later refresh must be idempotent, not a duplicate."""
    identity = {
        "ticker": ticker,
        "url": url,
        "title": title,
        "published_at": published_at.isoformat(),
    }
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


def _is_valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def parse_news_article(raw: dict[str, Any], *, ticker: str, provider: str) -> NewsArticle | None:
    """Validate one raw article dict into a `NewsArticle`, or None if it is
    missing/malformed required data. Never guesses a missing title, url, or
    published_at -- an invalid record is dropped by the caller, never
    coerced with a fabricated value. `raw` is expected to already carry
    normalized `title`/`url`/`publisher`/`summary`/`published_at` keys (the
    provider's own responsibility -- see `YFinanceProvider.get_news`); this
    function only validates and stamps identity, it does not itself
    interpret provider-specific JSON shapes.
    """
    title = raw.get("title")
    url = raw.get("url")
    published_at = raw.get("published_at")

    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str) or not _is_valid_url(url):
        return None
    if not isinstance(published_at, datetime):
        return None

    published_at = published_at.astimezone(UTC).replace(tzinfo=None) if published_at.tzinfo else published_at

    publisher = raw.get("publisher")
    publisher = publisher if isinstance(publisher, str) and publisher.strip() else None
    summary = raw.get("summary")
    summary = summary if isinstance(summary, str) and summary.strip() else None

    return NewsArticle(
        ticker=ticker,
        content_hash=compute_content_hash(ticker=ticker, url=url, title=title, published_at=published_at),
        title=title,
        publisher=publisher,
        url=url,
        summary=summary,
        published_at=published_at,
        provider=provider,
        raw_payload=raw.get("raw", raw),
    )
