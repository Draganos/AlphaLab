"""Tests for NewsImpact (alpha_lab.news.impact): deterministic topic tags
over already-stored News articles. No sentiment, no score, no conviction --
see the module's own docstring for the full scope."""

from datetime import datetime

from alpha_lab.database.models import NewsArticleRecord
from alpha_lab.news.impact import NewsImpactCategory, classify_article


def _article(*, title: str, summary: str | None = None, content_hash: str = "hash-1") -> NewsArticleRecord:
    return NewsArticleRecord(
        ticker="TEST",
        content_hash=content_hash,
        title=title,
        publisher="Fixture Wire",
        url="https://example.com/article",
        summary=summary,
        published_at=datetime(2024, 1, 5, 9, 0),
        retrieved_at=datetime(2024, 1, 5, 10, 0),
        provider="fixture",
        raw_payload={},
    )


def test_earnings_guidance_is_tagged_from_title():
    article = _article(title="Company reports strong quarterly earnings, raises guidance")
    result = classify_article(article)
    assert result.is_classified is True
    categories = {tag.category for tag in result.tags}
    assert NewsImpactCategory.EARNINGS_GUIDANCE in categories


def test_analyst_action_is_tagged_and_matched_phrases_are_verbatim():
    article = _article(title="Analyst upgrades stock, raises price target")
    result = classify_article(article)
    tag = next(t for t in result.tags if t.category == NewsImpactCategory.ANALYST_ACTION)
    assert "upgrade" in tag.matched_phrases
    assert "price target" in tag.matched_phrases
    # Every matched phrase must be a real, verbatim substring of the
    # searchable text -- never a fabricated or paraphrased label.
    for phrase in tag.matched_phrases:
        assert phrase in article.title.lower()


def test_related_domains_are_attached_per_category():
    article = _article(title="Analyst upgrades stock, raises price target")
    result = classify_article(article)
    tag = next(t for t in result.tags if t.category == NewsImpactCategory.ANALYST_ACTION)
    assert set(tag.related_domains) == {"analysts", "revisions"}


def test_a_category_with_no_mapped_domain_reports_an_empty_tuple():
    article = _article(title="Company faces lawsuit and regulatory investigation")
    result = classify_article(article)
    tag = next(t for t in result.tags if t.category == NewsImpactCategory.REGULATORY_LEGAL)
    assert tag.related_domains == ()


def test_an_article_can_match_multiple_categories():
    article = _article(
        title="CEO steps down after earnings miss",
        summary="The company also announced a share repurchase program.",
    )
    result = classify_article(article)
    categories = {tag.category for tag in result.tags}
    assert NewsImpactCategory.MANAGEMENT in categories
    assert NewsImpactCategory.EARNINGS_GUIDANCE in categories
    assert NewsImpactCategory.CORPORATE_ACTION in categories


def test_an_unrelated_article_is_honestly_unclassified_not_guessed():
    article = _article(title="Local bakery celebrates its tenth anniversary")
    result = classify_article(article)
    assert result.is_classified is False
    assert result.tags == []


def test_classification_works_from_title_alone_when_summary_is_missing():
    """yfinance articles frequently have no summary -- classification must
    still run on the title, never fail or silently skip."""
    article = _article(title="Federal Reserve signals another rate hike", summary=None)
    result = classify_article(article)
    assert result.is_classified is True
    categories = {tag.category for tag in result.tags}
    assert NewsImpactCategory.MACRO in categories


def test_classification_is_deterministic():
    article = _article(title="Analyst upgrades stock and raises price target")
    first = classify_article(article)
    second = classify_article(article)
    assert first == second


def test_news_impact_carries_the_source_articles_content_hash():
    article = _article(title="Company reports strong quarterly earnings", content_hash="specific-hash-42")
    result = classify_article(article)
    assert result.content_hash == "specific-hash-42"
