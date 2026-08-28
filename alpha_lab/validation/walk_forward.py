"""Expanding-window validation with strictly separated train/test intervals."""

from dataclasses import dataclass
from datetime import date
from collections.abc import Callable


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: date
    train_end: date
    test_start: date
    test_end: date

    def __post_init__(self) -> None:
        if not self.train_start <= self.train_end < self.test_start <= self.test_end:
            raise ValueError("Walk-forward train and test periods must be ordered and non-overlapping")


@dataclass(frozen=True)
class WalkForwardResult:
    fold: WalkForwardFold
    in_sample: dict[str, float | None]
    out_of_sample: dict[str, float | None]
    sharpe_degradation: float | None
    conclusion: str


def expanding_folds(train_start_year: int, first_test_year: int, last_test_year: int) -> list[WalkForwardFold]:
    if first_test_year > last_test_year or train_start_year >= first_test_year:
        raise ValueError("Invalid expanding-window years")
    return [WalkForwardFold(date(train_start_year, 1, 1), date(year - 1, 12, 31),
                            date(year, 1, 1), date(year, 12, 31))
            for year in range(first_test_year, last_test_year + 1)]


def evaluate_walk_forward(folds: list[WalkForwardFold],
                          evaluator: Callable[[date, date], dict[str, float | None]]) -> list[WalkForwardResult]:
    results = []
    for fold in folds:
        in_sample = evaluator(fold.train_start, fold.train_end)
        out_of_sample = evaluator(fold.test_start, fold.test_end)
        in_sharpe, out_sharpe = in_sample.get("sharpe"), out_of_sample.get("sharpe")
        meaningful = (in_sharpe is not None and out_sharpe is not None
                      and in_sharpe > 0 and out_sharpe > 0)
        degradation = out_sharpe / in_sharpe if meaningful else None
        conclusion = ("No repeatable edge demonstrated." if degradation is None or degradation < 0.5
                      else "Out-of-sample performance retained; continued validation required.")
        results.append(WalkForwardResult(fold, in_sample, out_of_sample, degradation, conclusion))
    return results
