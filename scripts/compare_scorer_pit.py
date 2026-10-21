#!/usr/bin/env python
"""Quantify what the point-in-time scorer fix changed, on your own database.

Re-scores the tracked universe on a grid of past dates twice -- with the
previous price selection (the newest stored version of every bar, whatever the
date) and with the corrected one (the version AlphaLab held on that date) --
and reports how many scores moved, by how much, and whether rankings and the
top-N changed. It also reports the effect of ranking against the tracked
universe recorded for each date instead of today's set (only dates after the
first recorded snapshot can be compared; earlier dates have no history). With `--backtests` it also re-runs every backtest saved in the
`backtest_runs` table under both selections and compares their metrics.

    python scripts/compare_scorer_pit.py [--db sqlite:///data/alpha_lab.db]
        [--start 2025-10-01 --end 2026-09-30 --step-days 30 --top-n 5] [--backtests]

Reads only (nothing is written, saved backtests are not modified). When the
database holds no revised bars the two methods are identical and every delta is
zero; that is the expected, honest result for a database filled in one go.
"""

import argparse
import json
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alpha_lab.backtest.database_runner import run_database_backtest
from alpha_lab.config import Settings, load_settings
from alpha_lab.database import make_engine
from alpha_lab.database.models import BacktestRun, Security
from alpha_lab.database.queries import latest_price_per_date_statement
from alpha_lab.research import load_universe
from alpha_lab.strategy import HistoricalScoringService
from alpha_lab.strategy import historical as historical_module
from alpha_lab.universe import BASIS_SNAPSHOT, tracked_universe_as_of


def _previous_price_selection(session, as_of, *conditions, live=None):
    """The scorer's price read before the fix: newest version of each bar."""
    return list(session.scalars(latest_price_per_date_statement(*conditions)))


@contextmanager
def previous_method():
    with mock.patch.object(historical_module, "price_history_as_known", _previous_price_selection):
        yield


def tracked_tickers(engine: Engine) -> list[str]:
    with Session(engine) as session:
        return list(session.scalars(select(Security.ticker).where(Security.is_tracked.is_(True)).order_by(Security.ticker)))


def revision_statistics(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        bars = connection.execute(text("SELECT COUNT(*) FROM (SELECT 1 FROM prices GROUP BY ticker, date)")).scalar()
        rows = connection.execute(text("SELECT COUNT(*) FROM prices")).scalar()
        multi = connection.execute(
            text("SELECT COUNT(*) FROM (SELECT 1 FROM prices GROUP BY ticker, date HAVING COUNT(*) > 1)")
        ).scalar()
    return {"price_rows": rows, "distinct_bars": bars, "bars_with_revisions": multi, "revision_rows": rows - bars}


def score_grid(start: date, end: date, step_days: int) -> list[date]:
    days, day = [], start
    while day <= end:
        days.append(day)
        day += timedelta(days=step_days)
    return days


def _scores(engine: Engine, settings: Settings, tickers: list[str], day: date) -> dict[str, float | None]:
    results = HistoricalScoringService(engine, settings).score_universe_as_of(
        day, tickers, min_score=0, minimum_coverage=0
    )
    return {item.ticker: item.score for item in results}


def _top_n(scores: dict[str, float | None], tickers: list[str], n: int) -> set[str]:
    return set(sorted(tickers, key=lambda t: (-scores[t], t))[:n])


def compare_scores(engine: Engine, settings: Settings, tickers: list[str], days: list[date], top_n: int) -> dict:
    per_date = []
    for day in days:
        with previous_method():
            before = _scores(engine, settings, tickers, day)
        after = _scores(engine, settings, tickers, day)
        both = sorted(t for t in before if before[t] is not None and after.get(t) is not None)
        diffs = {t: abs(after[t] - before[t]) for t in both}
        changed = [t for t, delta in diffs.items() if delta > 1e-9]
        if len(both) >= 3:
            rank_correlation = pd.Series({t: before[t] for t in both}).corr(
                pd.Series({t: after[t] for t in both}), method="spearman"
            )
        else:
            rank_correlation = None

        before_top, after_top = (_top_n(scores, both, top_n) for scores in (before, after))

        per_date.append({
            "date": day.isoformat(),
            "scored": len(both),
            "scores_changed": len(changed),
            "max_abs_score_change": round(max(diffs.values(), default=0.0), 6),
            "mean_abs_score_change": round(sum(diffs.values()) / len(diffs), 6) if diffs else 0.0,
            "spearman_rank_correlation": None if rank_correlation is None else round(float(rank_correlation), 6),
            "top_n_changed": len(before_top - after_top),
            "changed_tickers": changed,
        })
    return {
        "dates": per_date,
        "dates_with_any_change": sum(1 for row in per_date if row["scores_changed"]),
        "max_abs_score_change": max((row["max_abs_score_change"] for row in per_date), default=0.0),
        "dates_with_top_n_change": sum(1 for row in per_date if row["top_n_changed"]),
    }


def universe_history(engine: Engine) -> dict:
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT COUNT(*), MIN(recorded_at), MAX(recorded_at) FROM universe_snapshots")
            ).one()
    except OperationalError:
        return {"snapshots": 0, "note": "no universe_snapshots table: run any AlphaLab script once to create it"}
    return {"snapshots": row[0], "first_recorded_at": row[1], "last_recorded_at": row[2]}


def compare_universe(engine: Engine, settings: Settings, days: list[date], top_n: int) -> list[dict]:
    """For each date with a recorded membership that differs from today's: the
    default (tickers=None) scorer ranks against the recorded universe; the
    previous behaviour ranked against today's set. Dates before the first
    snapshot have no history and are reported as such, never reconstructed."""
    current = tracked_tickers(engine)
    rows = []
    for day in days:
        universe = tracked_universe_as_of(engine, day)
        if universe.basis != BASIS_SNAPSHOT:
            rows.append({"date": day.isoformat(), "basis": universe.basis, "compared": False})
            continue
        recorded = list(universe.tickers)
        if set(recorded) == set(current):
            rows.append({"date": day.isoformat(), "basis": universe.basis, "compared": False, "same_as_today": True})
            continue
        right, wrong = _scores(engine, settings, recorded, day), _scores(engine, settings, current, day)
        both = sorted(t for t in right if right[t] is not None and wrong.get(t) is not None)
        diffs = {t: abs(wrong[t] - right[t]) for t in both}
        rows.append({
            "date": day.isoformat(), "basis": universe.basis, "compared": True,
            "recorded_size": len(recorded), "today_size": len(current),
            "scores_changed": sum(1 for d in diffs.values() if d > 1e-9),
            "max_abs_score_change": round(max(diffs.values(), default=0.0), 6),
            "top_n_changed": len(_top_n(right, both, top_n) - _top_n(wrong, both, top_n)),
        })
    return rows


def _run_backtest(engine: Engine, settings: Settings, parameters: dict) -> dict:
    universe = load_universe(parameters["universe"])
    result, _ = run_database_backtest(
        engine, settings, list(universe.tickers), date.fromisoformat(parameters["start"]),
        date.fromisoformat(parameters["end"]), benchmark=parameters.get("benchmark", "SPY"),
        weighting=parameters.get("weighting"),
    )
    return {
        "metrics": {k: v for k, v in result.metrics.items()},
        "trades": len(result.trades),
        "selections": [(r.evaluation_date.isoformat(), list(r.selected)) for r in result.rebalances],
    }


def compare_backtests(engine: Engine, settings: Settings, runner: Callable = _run_backtest) -> list[dict]:
    with Session(engine) as session:
        saved = [(row.id, dict(row.parameters or {})) for row in session.scalars(select(BacktestRun).order_by(BacktestRun.id))]
    reports = []
    for run_id, parameters in saved:
        try:
            with previous_method():
                before = runner(engine, settings, parameters)
            after = runner(engine, settings, parameters)
        except (ValueError, KeyError, OSError) as error:  # a saved run may reference a missing universe file or benchmark
            reports.append({"run_id": run_id, "parameters": parameters, "error": f"{type(error).__name__}: {error}"})
            continue
        deltas = {}
        for key in sorted(set(before["metrics"]) | set(after["metrics"])):
            old, new = before["metrics"].get(key), after["metrics"].get(key)
            if isinstance(old, (int, float)) and isinstance(new, (int, float)):
                deltas[key] = {"before": old, "after": new, "change": new - old}
        reports.append({
            "run_id": run_id,
            "parameters": parameters,
            "metrics": deltas,
            "trades_before": before["trades"],
            "trades_after": after["trades"],
            "rebalances_with_different_selection": sum(
                1 for old, new in zip(before["selections"], after["selections"], strict=False) if old != new
            ),
            "rebalances": len(after["selections"]),
        })
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="", help="SQLite URL (default: the configured database)")
    today = date.today()
    parser.add_argument("--start", type=date.fromisoformat, default=today - timedelta(days=365))
    parser.add_argument("--end", type=date.fromisoformat, default=today - timedelta(days=1))
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--backtests", action="store_true", help="Re-run saved backtest_runs under both methods")
    options = parser.parse_args(argv)

    settings = load_settings()
    engine = make_engine(options.db or settings.database_url)
    try:
        report = {
            "revision_statistics": revision_statistics(engine),
            "scores": compare_scores(
                engine, settings, tracked_tickers(engine), score_grid(options.start, options.end, options.step_days),
                options.top_n,
            ),
            "universe_history": universe_history(engine),
            "universe_effect": compare_universe(
                engine, settings, score_grid(options.start, options.end, options.step_days), options.top_n
            ),
        }
        if options.backtests:
            report["backtests"] = compare_backtests(engine, settings)
        print(json.dumps(report, indent=2, default=str))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
