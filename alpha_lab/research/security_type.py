"""Security-Type Capability Model (PR #29).

AlphaLab's eight fundamental scoring categories (`alpha_lab.screener.
service.CATEGORY_EVIDENCE_METRICS`) were designed for an operating company
with its own income statement and balance sheet. An ETF has neither, so
several of those categories are not merely "missing evidence" for an ETF --
they are structurally meaningless for one, and always will be under
AlphaLab's current metric definitions:

* ``business_quality``/``financial_strength`` (margins, ROE/ROA, debt
  ratios) need a company's own income statement/balance sheet.
* ``earnings_growth``/``analyst_revisions`` need EPS/revenue and their
  analyst estimates -- an ETF has neither.
* ``valuation`` (P/E, EV/EBITDA, price/FCF) needs the same company-level
  earnings/EBITDA/FCF.
* ``shareholder_return`` here specifically means ``dividend_yield`` sourced
  from company fundamentals plus ``buyback_yield``/``total_shareholder_
  yield`` derived from share repurchases -- a corporate-action concept an
  ETF does not have (a fund's own distribution characteristics are a
  different, not-yet-implemented metric; see the project roadmap's PR #29
  spec). Confirmed empirically: FTEC/GDX show 0% coverage here today and
  always will via this pipeline, for exactly this structural reason, not a
  fetch failure.

``momentum`` (pure price history) and ``ai_research`` (attributable-
document commentary, already independently gated by `ai_attributable` and
left alone here) stay applicable to ETFs.

Before this module, all eight categories were treated as equally
applicable to every security type, so an ETF's `category_coverage`/
`overall_live_coverage`/confidence were diluted by six categories that can
never be filled -- not evidence that happened to be missing, but evidence
that was never expected. This module lets callers exclude those
categories from a coverage denominator instead of counting them as
missing, per the project roadmap's explicit rule: "A category that is not
applicable to a security type must not count against coverage as though
it were missing evidence."

Deliberately NOT touched by this module: `alpha_lab.ratings`/
`alpha_lab.screener.service`'s category *score* formulas, weights, or
minimum-metric thresholds (`CATEGORY_MINIMUM_METRICS`) -- those already
return `None` for a category with too little evidence regardless of the
reason (see `_category_score`'s `available_weight` exclusion), so
`overall_score` for an ETF is unaffected by this module entirely. Only
the separate *coverage*/*confidence* honesty metrics change, and only via
each call site explicitly opting in (see `alpha_lab.screener.service.
_record` and `alpha_lab.research.build._confidence_factors`) -- this
module computes nothing on its own and mutates nothing.

Extensibility: `SecurityType` has a third member, `OTHER`, covering every
`asset_type` this module does not yet recognize (macro-proxy indices,
futures, currencies, mutual funds, ...). `OTHER` deliberately maps to an
empty not-applicable set -- the same "nothing excluded" behavior as
`EQUITY` -- so an unrecognized or future security type is never silently
penalized by a category-applicability rule nobody has actually reviewed
for it. A future phase can give `OTHER` (or a new dedicated member) its
own capability set once that security type's evidence expectations are
actually specified, the same way this phase specifies ETF's.
"""

from enum import StrEnum


class SecurityType(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    # Anything not yet classified (indices, futures, currencies, mutual
    # funds, ...) -- see module docstring's Extensibility note.
    OTHER = "OTHER"


# Raw `Security.asset_type`/`LiveResearchRecord.asset_type` values, as
# populated by `YFinanceProvider` from Yahoo's own `quoteType` field
# (already uppercase: "EQUITY", "ETF", "INDEX", "FUTURE", ...).
_ASSET_TYPE_TO_SECURITY_TYPE = {
    "EQUITY": SecurityType.EQUITY,
    "ETF": SecurityType.ETF,
}


def normalize_security_type(asset_type: str | None) -> SecurityType:
    """Never raises: an unrecognized or missing `asset_type` maps to
    `SecurityType.OTHER`, which excludes nothing (see module docstring)."""
    if asset_type is None:
        return SecurityType.OTHER
    return _ASSET_TYPE_TO_SECURITY_TYPE.get(asset_type.upper(), SecurityType.OTHER)


# Fundamental categories (alpha_lab.screener.service.CATEGORY_EVIDENCE_METRICS
# keys) that are structurally not applicable per security type -- see module
# docstring for the ETF rationale category by category. EQUITY/OTHER exclude
# nothing, preserving today's behavior exactly.
NOT_APPLICABLE_CATEGORIES: dict[SecurityType, frozenset[str]] = {
    SecurityType.EQUITY: frozenset(),
    SecurityType.ETF: frozenset(
        {
            "business_quality",
            "earnings_growth",
            "financial_strength",
            "valuation",
            "analyst_revisions",
            "shareholder_return",
        }
    ),
    SecurityType.OTHER: frozenset(),
}


def is_category_applicable(category: str, security_type: SecurityType) -> bool:
    return category not in NOT_APPLICABLE_CATEGORIES[security_type]
