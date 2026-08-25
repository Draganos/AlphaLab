import pytest
from alpha_lab.strategy import composite_score, interpretation


def test_score_is_transparent_and_renormalizes_missing_data():
    result = composite_score({"earnings": 80, "momentum": None}, {"earnings": .5, "momentum": .5})
    assert result.score == 80
    assert result.coverage == .5
    assert result.contributions == {"earnings": 80}
    assert result.unavailable == ["momentum"]
    assert interpretation(result.score) == "Strong"


def test_invalid_weights_rejected():
    with pytest.raises(ValueError):
        composite_score({"earnings": 50}, {"earnings": .9})
