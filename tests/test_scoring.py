import pytest
from datetime import date
from alpha_lab.config import CoverageSettings
from alpha_lab.strategy import composite_score, configuration_hash, interpretation

THRESHOLDS = CoverageSettings(insufficient_below=.4, full_confidence_at=.7)


def test_score_is_transparent_and_renormalizes_missing_data():
    result = composite_score({"earnings": 80, "momentum": None}, {"earnings": .5, "momentum": .5},
                             date(2025, 1, 1), THRESHOLDS)
    assert result.score == 80
    assert result.coverage == .5
    assert result.contributions == {"earnings": 80}
    assert result.unavailable == ["momentum"]
    assert interpretation(result.score) == "Strong"


def test_invalid_weights_rejected():
    with pytest.raises(ValueError):
        composite_score({"earnings": 50}, {"earnings": .9}, date(2025, 1, 1), THRESHOLDS)


def test_scoring_is_repeatable_and_hash_is_order_independent():
    weights_a = {"earnings": .6, "momentum": .4}
    weights_b = {"momentum": .4, "earnings": .6}
    first = composite_score({"earnings": 70, "momentum": 80}, weights_a, date(2025, 1, 1), THRESHOLDS)
    second = composite_score({"momentum": 80, "earnings": 70}, weights_b, date(2025, 1, 1), THRESHOLDS)
    assert first == second
    assert configuration_hash(weights_a) == configuration_hash(weights_b)


def test_nan_is_missing_and_out_of_range_is_rejected():
    result = composite_score({"earnings": float("nan"), "momentum": 50},
                             {"earnings": .5, "momentum": .5}, date(2025, 1, 1), THRESHOLDS)
    assert result.score == 50
    assert result.unavailable == ["earnings"]
    with pytest.raises(ValueError):
        composite_score({"earnings": 101}, {"earnings": 1}, date(2025, 1, 1), THRESHOLDS)


@pytest.mark.parametrize(("coverage", "expected"), [
    (.3999, "Insufficient data"),
    (.40, "Provisional — Exceptional candidate"),
    (.6999, "Provisional — Exceptional candidate"),
    (.70, "Exceptional candidate"),
])
def test_coverage_confidence_boundaries(coverage, expected):
    weights = {"available": coverage, "missing": 1 - coverage}
    result = composite_score({"available": 85, "missing": None}, weights,
                             date(2025, 1, 1), THRESHOLDS)
    assert result.score == 85
    assert result.raw_interpretation == "Exceptional candidate"
    assert result.confidence_label == expected
