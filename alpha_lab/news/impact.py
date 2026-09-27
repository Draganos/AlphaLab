"""NewsImpact: deterministic topical relation tags for stored News articles.

Scope (approved before implementation, per the roadmap's post-#47
sequencing comment): purely a topic-tagging read layer over already-stored
`NewsArticleRecord`s -- title/summary text substring-matched against small,
reviewable phrase lexicons, mirroring `alpha_lab.ai.rule_based`'s existing
lexicon-matching idiom exactly (the same "deterministic, not ML, always
reproducible" reasoning applies here). Still no sentiment, no relevance
score, no conviction adjustment, and no code path into
`alpha_lab.research`/`.screener`/`.strategy`/`.backtest`/`.portfolio`/
`.ratings`/`.factors` -- News Engine's own non-integration guarantee is
unchanged; this only adds a topic label on top of it.

Deliberately a pure function of already-in-hand article text, not a new
persisted table: `classify_article` takes a `NewsArticleRecord` (already a
point-in-time-safe read via `NewsService.get_history(..., as_of=...)`) and
returns a `NewsImpact` computed on the spot, the same "pure construction
from already-computed objects" pattern
`alpha_lab.research_stance.build_research_stance` uses. Because the
classification is a deterministic function of immutable text
(`NewsArticleRecord` rows are never mutated once written -- see its own
docstring), the same article always yields the same `NewsImpact`; there is
nothing to version or leak across a historical `as_of` boundary that
`NewsArticleRecord.retrieved_at`'s own PIT gate doesn't already cover.

Missing-data honesty: many providers (yfinance included) return articles
with a title but no summary. Classification runs on whatever text exists
(title, or title+summary) -- a category with zero phrase matches is simply
absent from `NewsImpact.tags`, never guessed. An article matching nothing
at all is reported as `NewsImpact.is_classified = False`, a real, honest
outcome, not an error.

Category-to-domain labels below name `alpha_lab.research_stance.stance`'s
own domain keys (`DOMAIN_ORDER`) purely as static string labels for
cross-reference -- this module does not import that package or any other
evidence domain, keeping NewsImpact's only real dependency the News Engine
itself.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from alpha_lab.database.models import NewsArticleRecord

NEWS_IMPACT_METHODOLOGY_VERSION = "news-impact-v1"


class NewsImpactCategory(StrEnum):
    EARNINGS_GUIDANCE = "EARNINGS_GUIDANCE"
    ANALYST_ACTION = "ANALYST_ACTION"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    REGULATORY_LEGAL = "REGULATORY_LEGAL"
    MACRO = "MACRO"
    MANAGEMENT = "MANAGEMENT"
    PRODUCT_STRATEGIC = "PRODUCT_STRATEGIC"
    PRICE_ACTION = "PRICE_ACTION"


# Deliberately modest, reviewable phrase lists -- a V1 heuristic exactly
# like `alpha_lab.ai.rule_based._LEXICON`, not an exhaustive taxonomy.
# Lowercase, matched as case-insensitive substrings against `title` +
# `summary` (see `_searchable_text`).
_LEXICON: dict[NewsImpactCategory, tuple[str, ...]] = {
    NewsImpactCategory.EARNINGS_GUIDANCE: (
        "earnings", "quarterly results", "revenue", "guidance", "eps",
        "profit warning", "beats estimates", "misses estimates", "outlook",
    ),
    NewsImpactCategory.ANALYST_ACTION: (
        "upgrade", "downgrade", "price target", "initiated coverage",
        "reiterates", "analyst", "overweight", "underweight",
    ),
    NewsImpactCategory.CORPORATE_ACTION: (
        "acquisition", "acquires", "merger", "buyback", "share repurchase",
        "dividend", "spinoff", "spin-off", "divestiture", "bankruptcy",
        "ipo", "stock split",
    ),
    NewsImpactCategory.REGULATORY_LEGAL: (
        "lawsuit", "investigation", "antitrust", "sec filing", "regulatory",
        "fine", "settlement", "probe", "litigation",
    ),
    NewsImpactCategory.MACRO: (
        "federal reserve", "interest rate", "inflation", "recession", "gdp",
        "tariff", "fed ", "rate hike", "rate cut", "unemployment",
    ),
    NewsImpactCategory.MANAGEMENT: (
        "ceo", "cfo", "chief executive", "chief financial officer",
        "resigns", "appoints", "steps down", "names new",
    ),
    NewsImpactCategory.PRODUCT_STRATEGIC: (
        "launches", "partnership", "unveils", "expansion", "new product",
        "collaborat",
    ),
    NewsImpactCategory.PRICE_ACTION: (
        "52-week high", "52-week low", "all-time high", "rally", "plunge",
        "selloff", "sell-off", "surges", "tumbles",
    ),
}

# Static cross-reference labels only -- matches
# `alpha_lab.research_stance.stance.DOMAIN_ORDER`'s own domain keys by
# convention; not imported from there (see module docstring).
_RELATED_DOMAINS: dict[NewsImpactCategory, tuple[str, ...]] = {
    NewsImpactCategory.EARNINGS_GUIDANCE: ("fundamentals",),
    NewsImpactCategory.ANALYST_ACTION: ("analysts", "revisions"),
    NewsImpactCategory.CORPORATE_ACTION: ("fundamentals",),
    NewsImpactCategory.REGULATORY_LEGAL: (),
    NewsImpactCategory.MACRO: ("macro",),
    NewsImpactCategory.MANAGEMENT: (),
    NewsImpactCategory.PRODUCT_STRATEGIC: ("fundamentals",),
    NewsImpactCategory.PRICE_ACTION: ("technical",),
}


class NewsImpactTag(BaseModel):
    category: NewsImpactCategory
    # Verbatim phrases actually found (lowercased, as matched) -- never a
    # fabricated or paraphrased label; always traceable back to real text.
    matched_phrases: list[str]
    # Static labels only, see module docstring -- may be empty (a real
    # topic with no existing AlphaLab domain to cross-reference, e.g.
    # REGULATORY_LEGAL/MANAGEMENT).
    related_domains: tuple[str, ...]


class NewsImpact(BaseModel):
    content_hash: str
    tags: list[NewsImpactTag] = Field(default_factory=list)
    is_classified: bool
    methodology_version: str = NEWS_IMPACT_METHODOLOGY_VERSION


def _searchable_text(article: NewsArticleRecord) -> str:
    parts = [article.title]
    if article.summary:
        parts.append(article.summary)
    return " ".join(parts).lower()


def classify_article(article: NewsArticleRecord) -> NewsImpact:
    """Deterministic, stateless: the same article always produces the same
    `NewsImpact`. Reads only `title`/`summary` -- never the provider's
    `raw_payload`, since that is not guaranteed to be structured the same
    way across providers (see `alpha_lab.news.article`'s own schema-caveat
    reasoning)."""
    text = _searchable_text(article)
    tags: list[NewsImpactTag] = []
    for category, phrases in _LEXICON.items():
        matched = [phrase for phrase in phrases if phrase in text]
        if matched:
            tags.append(NewsImpactTag(
                category=category,
                matched_phrases=matched,
                related_domains=_RELATED_DOMAINS[category],
            ))
    return NewsImpact(
        content_hash=article.content_hash,
        tags=tags,
        is_classified=bool(tags),
    )
