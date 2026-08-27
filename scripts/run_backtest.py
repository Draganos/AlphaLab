#!/usr/bin/env python
"""Run an auditable Phase 2 database backtest."""

import argparse
from datetime import date
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session
from alpha_lab.backtest.database_runner import run_database_backtest
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import BacktestRun
from alpha_lab.research import load_universe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--benchmark", choices=["SPY", "QQQ", "VT"], default="SPY")
    parser.add_argument("--universe", default="data/universes/us_research_sample.csv")
    parser.add_argument("--weighting", choices=["equal", "score", "inverse_volatility"], default="equal")
    args = parser.parse_args()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        create_schema(engine)
        universe = load_universe(args.universe)
        result, _ = run_database_backtest(engine, settings, list(universe.tickers), args.start, args.end,
                                          benchmark=args.benchmark, weighting=args.weighting)
        parameters = {"start": args.start.isoformat(), "end": args.end.isoformat(),
                      "benchmark": args.benchmark, "universe": args.universe,
                      "weighting": args.weighting, "limitation": universe.limitation}
        with Session(engine) as session:
            session.add(BacktestRun(parameters=parameters, metrics=result.metrics))
            session.commit()
        print(json.dumps({"metrics": result.metrics, "trades": len(result.trades),
                          "limitation": universe.limitation}, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
