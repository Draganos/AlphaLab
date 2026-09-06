"""Capability model coverage for the new Analyst Consensus / Technical
Summary evidence fields -- distinguishes provider-supports-metric from
provider-returned-metric/metric-was-valid, which the capability model
itself doesn't track (that's the per-refresh coverage the domain objects
themselves report); this only tests the static capability declaration."""

from alpha_lab.providers.capabilities import (
    CAPABILITY_FIELDS,
    Capability,
    capability,
    provider_capability_matrix,
)


def test_new_capability_fields_are_declared():
    for field in (
        "analyst_recommendations",
        "analyst_recommendation_summary",
        "analyst_price_targets",
        "technical_price_history",
    ):
        assert field in CAPABILITY_FIELDS


def test_yfinance_provider_analyst_fields_are_partial_not_reliable():
    """Coverage is real but genuinely inconsistent across tickers -- must
    not be overstated as RELIABLE_CURRENT."""
    for field in ("analyst_recommendations", "analyst_recommendation_summary", "analyst_price_targets"):
        assert capability("YFinanceProvider", field) == Capability.PARTIAL


def test_yfinance_provider_technical_price_history_is_reliable():
    assert capability("YFinanceProvider", "technical_price_history") == Capability.RELIABLE_CURRENT


def test_unrelated_providers_fail_closed_to_unsupported():
    for provider in ("SECCompanyFactsProvider", "NasdaqTraderUniverseProvider", "AlphaLabEstimateSnapshots"):
        assert capability(provider, "analyst_recommendations") == Capability.UNSUPPORTED
        assert capability(provider, "technical_price_history") == Capability.UNSUPPORTED


def test_capability_matrix_includes_the_new_fields_for_every_provider():
    matrix = provider_capability_matrix()
    for provider_fields in matrix.values():
        assert "analyst_recommendations" in provider_fields
        assert "technical_price_history" in provider_fields
