"""Deterministic, offline tests for alpha_lab.research.security_type."""

from alpha_lab.research.security_type import (
    NOT_APPLICABLE_CATEGORIES,
    SecurityType,
    is_category_applicable,
    normalize_security_type,
)


def test_normalize_recognizes_equity_and_etf_case_insensitively():
    assert normalize_security_type("EQUITY") == SecurityType.EQUITY
    assert normalize_security_type("equity") == SecurityType.EQUITY
    assert normalize_security_type("ETF") == SecurityType.ETF
    assert normalize_security_type("etf") == SecurityType.ETF


def test_normalize_maps_unknown_or_missing_to_other():
    assert normalize_security_type(None) == SecurityType.OTHER
    assert normalize_security_type("INDEX") == SecurityType.OTHER
    assert normalize_security_type("FUTURE") == SecurityType.OTHER
    assert normalize_security_type("MUTUALFUND") == SecurityType.OTHER
    assert normalize_security_type("") == SecurityType.OTHER


def test_equity_excludes_nothing():
    assert NOT_APPLICABLE_CATEGORIES[SecurityType.EQUITY] == frozenset()


def test_other_excludes_nothing():
    """Extensibility default: a security type this module does not yet
    classify must never be silently penalized by an applicability rule
    nobody has reviewed for it."""
    assert NOT_APPLICABLE_CATEGORIES[SecurityType.OTHER] == frozenset()


def test_etf_excludes_exactly_the_company_financial_statement_categories():
    assert NOT_APPLICABLE_CATEGORIES[SecurityType.ETF] == frozenset(
        {
            "business_quality",
            "earnings_growth",
            "financial_strength",
            "valuation",
            "analyst_revisions",
            "shareholder_return",
            "ai_research",
        }
    )


def test_only_momentum_remains_applicable_to_etfs():
    assert is_category_applicable("momentum", SecurityType.ETF)
    assert not is_category_applicable("ai_research", SecurityType.ETF)


def test_ai_research_stays_applicable_to_equities_and_other():
    """The document-commentary category is only structurally inapplicable
    to a fund; an equity without filings ingested is a genuine gap."""
    assert is_category_applicable("ai_research", SecurityType.EQUITY)
    assert is_category_applicable("ai_research", SecurityType.OTHER)


def test_is_category_applicable_matches_the_not_applicable_set():
    for security_type in SecurityType:
        for category in (
            "business_quality", "earnings_growth", "financial_strength",
            "valuation", "analyst_revisions", "shareholder_return",
            "momentum", "ai_research",
        ):
            expected = category not in NOT_APPLICABLE_CATEGORIES[security_type]
            assert is_category_applicable(category, security_type) == expected
