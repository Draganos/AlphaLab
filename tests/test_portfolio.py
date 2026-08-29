from alpha_lab.portfolio import Candidate, construct_portfolio


def test_coverage_and_other_exclusions_are_explicit():
    candidates = [Candidate("LOW", 95, .15), Candidate("GOOD", 80, .70),
                  Candidate("NOPRICE", 99, 1, has_price=False)]
    result = construct_portfolio(candidates, method="equal", min_score=70, minimum_coverage=.70,
                                 min_positions=1, max_positions=10, max_position=1, max_sector=None)
    assert result.weights == {"GOOD": 1}
    assert result.excluded == {"LOW": "insufficient coverage", "NOPRICE": "missing price"}


def test_position_sector_and_cash_constraints():
    candidates = [Candidate("A", 90, 1, "Tech"), Candidate("B", 80, 1, "Tech"),
                  Candidate("C", 70, 1, "Finance")]
    result = construct_portfolio(candidates, method="equal", min_score=0, minimum_coverage=0,
                                 min_positions=1, max_positions=2, max_position=.4, max_sector=.5)
    assert len(result.weights) == 2
    assert max(result.weights.values()) <= .4
    assert sum(result.weights.values()) <= .5
    assert result.excluded["C"] == "position limit"
    assert result.cash_weight >= .5


def test_not_enough_qualifiers_means_all_cash():
    result = construct_portfolio([Candidate("A", 90, 1)], method="score", min_score=70,
        minimum_coverage=.7, min_positions=2, max_positions=5, max_position=.2, max_sector=None)
    assert result.weights == {"A": .2}
    assert result.cash_weight == .8
    assert result.notes


def test_inverse_volatility_uses_only_supplied_historical_values():
    result = construct_portfolio([Candidate("LOW", 80, 1, volatility=.1),
                                  Candidate("HIGH", 80, 1, volatility=.2)],
        method="inverse_volatility", min_score=0, minimum_coverage=0, min_positions=1,
        max_positions=2, max_position=1, max_sector=None)
    assert result.weights["LOW"] == 2 / 3
