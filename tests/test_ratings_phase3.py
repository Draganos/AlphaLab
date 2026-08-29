from datetime import date
import pytest
import pandas as pd

from alpha_lab.ratings import (
    calculate_coverage,
    calculate_quality_factors,
    calculate_revision_factors,
)
from alpha_lab.ratings.valuation import calculate_valuation_factors
from alpha_lab.screener.service import _live_percentiles


def test_valuation_invalid_values_stay_missing_and_lower_pe_is_better():
    negative = calculate_valuation_factors(
        price=10,
        shares=10,
        eps=-1,
        forward_eps=None,
        revenue=50,
        ebitda=10,
        free_cash_flow=0,
        debt=0,
        cash=0,
    )
    assert negative["pe"] is None
    assert negative["price_fcf"] is None
    cheap = calculate_valuation_factors(
        price=10,
        shares=10,
        eps=2,
        forward_eps=None,
        revenue=50,
        ebitda=10,
        free_cash_flow=5,
        debt=0,
        cash=0,
    )
    expensive = calculate_valuation_factors(
        price=20,
        shares=10,
        eps=2,
        forward_eps=None,
        revenue=50,
        ebitda=10,
        free_cash_flow=5,
        debt=0,
        cash=0,
    )
    assert cheap["pe"] < expensive["pe"]


def test_revisions_require_timestamped_prior_and_exclude_future():
    data = pd.DataFrame(
        [
            {"observation_date": "2024-01-01", "consensus_eps": 1.0},
            {"observation_date": "2024-01-31", "consensus_eps": 1.2},
            {"observation_date": "2025-01-01", "consensus_eps": 99.0},
        ]
    )
    early = calculate_revision_factors(data.iloc[:1], date(2024, 1, 1))
    result = calculate_revision_factors(data, date(2024, 1, 31))
    assert early["eps_revision_30d"] is None
    assert result["current_consensus_eps"] == 1.2
    assert result["eps_revision_30d"] == pytest.approx(0.2)


def test_single_old_snapshot_cannot_serve_as_its_own_revision_prior():
    data = pd.DataFrame([{"observation_date": "2026-01-01", "consensus_eps": 1.0}])
    result = calculate_revision_factors(data, date(2026, 3, 1))
    assert all(result[f"eps_revision_{days}d"] is None for days in (7, 30, 90))


def test_two_distinct_snapshots_produce_revision_without_future_leakage():
    data = pd.DataFrame(
        [
            {"observation_date": "2026-01-01", "consensus_eps": 1.0},
            {"observation_date": "2026-02-01", "consensus_eps": 1.25},
            {"observation_date": "2026-04-01", "consensus_eps": 99.0},
        ]
    )
    result = calculate_revision_factors(data, date(2026, 3, 1))
    assert result["current_consensus_eps"] == 1.25
    assert result["eps_revision_30d"] == pytest.approx(0.25)
    assert result["eps_revision_90d"] is None


def test_zero_prior_estimate_is_unavailable_not_infinite():
    data = pd.DataFrame(
        [
            {"observation_date": "2026-01-01", "consensus_eps": 0.0},
            {"observation_date": "2026-02-01", "consensus_eps": 1.0},
        ]
    )
    assert (
        calculate_revision_factors(data, date(2026, 3, 1))["eps_revision_30d"] is None
    )


def test_revision_calculation_never_mixes_estimate_providers():
    data = pd.DataFrame(
        {
            "observation_date": [date(2026, 1, 1), date(2026, 3, 1)],
            "consensus_eps": [1.0, 10.0],
            "provider": ["A", "B"],
        }
    )
    result = calculate_revision_factors(data, date(2026, 3, 1))
    assert result["current_consensus_eps"] == 10
    assert result["eps_revision_90d"] is None


def test_coverage_and_quality_missing_are_not_zero():
    weights = {"growth": 0.5, "ai_research": 0.5}
    low = calculate_coverage(
        {"growth": 0.5, "ai_research": 0.0},
        weights,
        ai_available=False,
        historical_available_weight=0.25,
    )
    full = calculate_coverage(
        {"growth": 1.0, "ai_research": 1.0},
        weights,
        ai_available=True,
        historical_available_weight=0.25,
    )
    assert low.overall_live == 0.25 and full.overall_live == 1
    assert low.historical == 0.25
    assert calculate_quality_factors({"revenue": 100})["gross_margin"] is None


def test_excluded_extreme_does_not_contaminate_investable_percentiles():
    raw = pd.DataFrame({"pe": [10.0, 20.0, 0.01]}, index=["A", "B", "EXCLUDED"])
    with_excluded = _live_percentiles(raw, ["A", "B"])
    without_excluded = _live_percentiles(raw.loc[["A", "B"]], ["A", "B"])
    assert with_excluded.loc["A", "pe"] == without_excluded.loc["A", "pe"]
    assert with_excluded.loc["EXCLUDED", "pe"] == 100
