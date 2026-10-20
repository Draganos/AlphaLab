"""scripts/compare_scorer_pit.py: the before/after quantification of the
point-in-time scorer fix."""

import importlib.util
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import BacktestRun, Price, Security

ROOT = Path(__file__).resolve().parents[1]
EVALUATION = date(2024, 12, 31)
FIRST_LOAD = datetime(2024, 12, 31, 20, 0, tzinfo=UTC)
REVISED = datetime(2025, 2, 1, 9, 0, tzinfo=UTC)


def _tool():
    spec = importlib.util.spec_from_file_location("compare_scorer_pit_script", ROOT / "scripts" / "compare_scorer_pit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine(*, revise: bool):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        for ticker, slope in (("A", 1.0), ("B", 0.5), ("C", 2.0), ("D", 0.1)):
            session.add(Security(ticker=ticker, sector="Test", is_tracked=True))
            start = EVALUATION - timedelta(days=299)
            for offset in range(300):
                value = 10 + slope * offset
                session.add(Price(ticker=ticker, date=start + timedelta(days=offset), open=value, close=value,
                                  adjusted_close=value, volume=1_000, provider="test", ingested_at=FIRST_LOAD))
            if revise and ticker in {"A", "B"}:  # a restatement stored after the grid date, reversing their order
                for offset in range(300):
                    value = (10 + slope * offset) * (0.3 if ticker == "A" else 3.0) * (1 + offset / 100)
                    session.add(Price(ticker=ticker, date=start + timedelta(days=offset), open=value, close=value,
                                      adjusted_close=value, volume=1_000, provider="test", ingested_at=REVISED))
        session.commit()
    return engine


def test_with_no_revisions_the_two_methods_are_identical():
    tool, engine = _tool(), _engine(revise=False)
    settings = load_settings()
    report = tool.compare_scores(engine, settings, tool.tracked_tickers(engine), [EVALUATION, date(2025, 3, 1)], 2)
    assert report["dates_with_any_change"] == 0 and report["max_abs_score_change"] == 0.0
    assert tool.revision_statistics(engine) == {"price_rows": 1200, "distinct_bars": 1200,
                                                "bars_with_revisions": 0, "revision_rows": 0}
    engine.dispose()


def test_revisions_move_only_the_dates_they_postdate_and_the_report_counts_them():
    tool, engine = _tool(), _engine(revise=True)
    settings = load_settings()
    before_revision, after_revision = EVALUATION, date(2025, 3, 1)
    report = tool.compare_scores(engine, settings, tool.tracked_tickers(engine), [before_revision, after_revision], 2)
    early, late = report["dates"]
    # on the early date the old method let the later restatement leak in; the fixed one does not
    assert early["scores_changed"] > 0 and early["max_abs_score_change"] > 0
    assert set(early["changed_tickers"]) <= {"A", "B", "C", "D"}
    # on the later date the restatement was already held, so both methods agree
    assert late["scores_changed"] == 0
    assert report["dates_with_any_change"] == 1
    stats = tool.revision_statistics(engine)
    assert stats["bars_with_revisions"] == 600 and stats["revision_rows"] == 600
    engine.dispose()


def test_saved_backtests_are_rerun_under_both_methods_and_failures_are_reported_not_raised():
    tool, engine = _tool(), _engine(revise=True)
    with Session(engine) as session:
        session.add(BacktestRun(parameters={"start": "2024-06-01", "end": "2024-12-30", "universe": "missing.csv"}, metrics={}))
        session.commit()
    [report] = tool.compare_backtests(engine, load_settings())
    assert report["run_id"] == 1 and "error" in report

    def fake_runner(engine, settings, parameters):
        from alpha_lab.strategy import historical

        leaks = historical.price_history_as_known.__name__ == "_previous_price_selection"
        return {"metrics": {"total_return": 0.2 if leaks else 0.1, "label": "x"}, "trades": 5 if leaks else 4,
                "selections": [("2024-07-01", ["A"] if leaks else ["B"])]}

    [compared] = tool.compare_backtests(engine, load_settings(), runner=fake_runner)
    assert compared["metrics"]["total_return"] == {"before": 0.2, "after": 0.1, "change": compared["metrics"]["total_return"]["change"]}
    assert abs(compared["metrics"]["total_return"]["change"] + 0.1) < 1e-9 and "label" not in compared["metrics"]
    assert (compared["trades_before"], compared["trades_after"]) == (5, 4)
    assert compared["rebalances_with_different_selection"] == 1
    engine.dispose()
