from datetime import date
import pytest
from alpha_lab.validation import WalkForwardFold, evaluate_walk_forward, expanding_folds


def test_expanding_folds_are_separate_and_combine():
    folds = expanding_folds(2020, 2023, 2025)
    assert len(folds) == 3
    assert all(fold.train_end < fold.test_start for fold in folds)
    calls = []
    def evaluate(start, end):
        calls.append((start, end))
        return {"sharpe": 1 if end.year < 2023 else .2, "total_return": .1}
    results = evaluate_walk_forward(folds, evaluate)
    assert len(calls) == 6 and len(results) == 3
    assert results[0].conclusion == "No repeatable edge demonstrated."


def test_overlapping_fold_rejected():
    with pytest.raises(ValueError):
        WalkForwardFold(date(2020, 1, 1), date(2023, 1, 1), date(2023, 1, 1), date(2024, 1, 1))


@pytest.mark.parametrize(("in_sharpe", "out_sharpe"), [
    (None, 1), (0, 1), (-1, 1), (1, 0), (1, -1), (-1, -1),
])
def test_non_positive_or_undefined_sharpes_never_claim_retention(in_sharpe, out_sharpe):
    fold = expanding_folds(2020, 2023, 2023)[0]
    values = iter([in_sharpe, out_sharpe])
    result = evaluate_walk_forward([fold], lambda _start, _end: {"sharpe": next(values)})[0]
    assert result.sharpe_degradation is None
    assert result.conclusion == "No repeatable edge demonstrated."
