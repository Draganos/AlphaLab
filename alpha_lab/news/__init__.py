"""AlphaLab News Engine (Phase 2C): a PIT-safe news evidence layer.

Scope (approved before implementation): ingestion -> validated/stored
evidence -> deterministic read exposure only. No sentiment, no relevance
score, no NewsImpact classification, no conviction, and no code path into
`alpha_lab.research`/`.screener`/`.strategy`/`.backtest`/`.portfolio`/
`.ratings`/`.factors`. See `alpha_lab.news.service`'s module docstring for
the full scope rationale and `ARCHITECTURE.md`'s News Engine section for
the PIT design.
"""

from alpha_lab.news.article import NewsArticle, compute_content_hash, parse_news_article
from alpha_lab.news.service import NewsService

__all__ = [
    "NewsArticle",
    "compute_content_hash",
    "parse_news_article",
    "NewsService",
]
