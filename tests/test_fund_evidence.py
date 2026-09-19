"""Deterministic, offline tests for alpha_lab.research.fund_evidence's
pure build_fund_evidence function. No network, no database -- a plain raw
dict stands in for YFinanceProvider.get_fund_data's output."""

from datetime import date

from alpha_lab.research.fund_evidence import build_fund_evidence


def _raw(**overrides) -> dict:
    base = {
        "ticker": "FTEC",
        "as_of": date(2026, 9, 18),
        "category_name": "Technology",
        "fund_family": "Fidelity Investments",
        "legal_type": "Exchange Traded Fund",
        "description": "A technology sector fund.",
        "cash_position": 0.0006,
        "stock_position": 0.9991,
        "bond_position": 0.0,
        "preferred_position": 0.0,
        "convertible_position": 0.0,
        "other_position": 0.0003,
        "sector_weightings": {"technology": 0.9933, "financial_services": 0.003},
        "expense_ratio": 0.00084,
        "category_avg_expense_ratio": 0.009,
        "holdings_turnover": 0.09,
        "total_net_assets": 691876.75,
        "price_earnings": 0.0311,
        "price_book": 0.09579,
        "price_sales": 0.13374,
        "price_cashflow": 0.03748,
        "top_holdings": [
            {"symbol": "NVDA", "name": "NVIDIA Corp", "weight": 0.177585},
            {"symbol": "AAPL", "name": "Apple Inc", "weight": 0.158266},
        ],
        "source": "YFinanceProvider",
    }
    base.update(overrides)
    return base


def test_returns_none_when_every_domain_is_absent():
    """Matches analyst_consensus/technical_summary's "None means not
    computed" convention -- never an empty-but-present object."""
    raw = _raw(
        cash_position=None, stock_position=None, bond_position=None,
        preferred_position=None, convertible_position=None, other_position=None,
        sector_weightings={}, expense_ratio=None, category_avg_expense_ratio=None,
        holdings_turnover=None, total_net_assets=None, price_earnings=None,
        price_book=None, price_sales=None, price_cashflow=None, top_holdings=[],
    )
    assert build_fund_evidence(raw) is None


def test_full_coverage_when_all_five_domains_present():
    evidence = build_fund_evidence(_raw())
    assert evidence.coverage == 1.0
    assert set(evidence.evidence_ids) == {
        "fund:asset_allocation", "fund:sector_weightings", "fund:operations",
        "fund:equity_holdings", "fund:top_holdings",
    }


def test_partial_coverage_when_only_some_domains_present():
    raw = _raw(sector_weightings={}, top_holdings=[])
    evidence = build_fund_evidence(raw)
    assert evidence.coverage == 3 / 5
    assert "fund:sector_weightings" not in evidence.evidence_ids
    assert "fund:top_holdings" not in evidence.evidence_ids


def test_asset_allocation_domain_is_present_even_with_only_one_field():
    """A domain with partial-but-real data (e.g. only stock_position
    reported) still counts as present -- never excluded just because
    every field in it isn't populated."""
    raw = _raw(
        cash_position=None, bond_position=None, preferred_position=None,
        convertible_position=None, other_position=None,
        # stock_position stays populated
        sector_weightings={}, expense_ratio=None, category_avg_expense_ratio=None,
        holdings_turnover=None, total_net_assets=None, price_earnings=None,
        price_book=None, price_sales=None, price_cashflow=None, top_holdings=[],
    )
    evidence = build_fund_evidence(raw)
    assert evidence is not None
    assert evidence.coverage == 1 / 5
    assert evidence.asset_allocation.stock == 0.9991
    assert evidence.asset_allocation.cash is None


def test_top_holdings_concentration_is_the_sum_of_reported_weights():
    evidence = build_fund_evidence(_raw())
    assert evidence.top_holdings_concentration == 0.177585 + 0.158266


def test_top_holdings_concentration_is_none_not_zero_when_no_holdings():
    raw = _raw(top_holdings=[])
    evidence = build_fund_evidence(raw)
    # Some other domain keeps it non-None overall, but concentration itself
    # must be a genuine None, never a fabricated 0.0.
    assert evidence.top_holdings_concentration is None


def test_a_genuine_zero_position_is_preserved_not_treated_as_missing():
    """bond_position=0.0 for a pure-equity fund is a real, reported zero --
    distinct from None (the field never being reported at all)."""
    evidence = build_fund_evidence(_raw())
    assert evidence.asset_allocation.bond == 0.0
    assert evidence.asset_allocation.is_present()


def test_equity_holdings_excludes_unreliable_fields_by_construction():
    """Median Market Cap / 3 Year Earnings Growth were confirmed unreliable
    (always <NA>) and were never given fields on EquityHoldingsCharacteristics
    -- there is nothing for build_fund_evidence to populate or fabricate."""
    evidence = build_fund_evidence(_raw())
    assert not hasattr(evidence.equity_holdings, "median_market_cap")
    assert not hasattr(evidence.equity_holdings, "three_year_earnings_growth")


def test_never_produces_a_second_overall_score():
    """FundEvidence is an evidence layer only -- it must never carry
    anything that looks like a new overall/composite score."""
    evidence = build_fund_evidence(_raw())
    assert not hasattr(evidence, "overall_score")
    assert not hasattr(evidence, "score")
    assert not hasattr(evidence, "composite_score")
    assert not hasattr(evidence, "rating")
