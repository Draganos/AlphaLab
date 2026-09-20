#!/usr/bin/env python
"""Report whether the new supplemental research signals correlate with a
ticker's own subsequent price return -- a read-only analysis, never wired
into any scoring or backtest path. See alpha_lab.analytics.signal_
predictive_value's module docstring for the full methodology and the
scope decision behind it (Technical Summary only today; Analyst
Consensus/AI Research Rating designed but gated on more history; Fund
Evidence excluded by design, not data depth)."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.analytics.signal_predictive_value import (  # noqa: E402
    CorrelationResult,
    InsufficientSnapshotHistory,
    correlate_ai_research_assessment_with_forward_returns,
    correlate_analyst_consensus_with_forward_returns,
    correlate_technical_summary_with_forward_returns,
)
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import make_engine  # noqa: E402
from alpha_lab.refresh import configured_universe_tickers  # noqa: E402


def _report(result: CorrelationResult) -> None:
    print(f"{result.signal_name} (forward {result.forward_days} trading days)")
    print(f"  sample size: {result.sample_size}")
    if result.pearson is None:
        if result.sample_size == 0:
            print(
                "  zero observations -- likely means every sampled historical date\n"
                "  predates this ticker's actual Price.ingested_at, not that the signal\n"
                "  lacks predictive value. Expected right after a bulk historical\n"
                "  backfill (every row shares roughly one real ingestion moment,\n"
                "  however far back its date is): this becomes testable once real,\n"
                "  incremental day-by-day ingestion has actually been running for at\n"
                "  least --forward-days trading days."
            )
        else:
            print("  not enough observations to compute a correlation")
        return
    print(f"  pearson:  {result.pearson:+.3f}")
    print(f"  spearman: {result.spearman:+.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", help="Tickers to analyze; defaults to the configured universe")
    parser.add_argument("--forward-days", type=int, default=20, help="Forward-return horizon in trading days")
    parser.add_argument(
        "--sample-interval-days", type=int, default=20,
        help="Technical Summary only: stride between sampled historical dates, in trading days",
    )
    args = parser.parse_args()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    tickers = args.tickers or configured_universe_tickers(engine)

    print(f"Universe: {len(tickers)} ticker(s)")
    print()

    technical = correlate_technical_summary_with_forward_returns(
        engine, tickers, forward_days=args.forward_days, sample_interval_days=args.sample_interval_days,
    )
    _report(technical)
    print()

    for label, correlate in (
        ("Analyst Consensus", correlate_analyst_consensus_with_forward_returns),
        ("AI Research Rating", correlate_ai_research_assessment_with_forward_returns),
    ):
        try:
            result = correlate(engine, settings, tickers, forward_days=args.forward_days)
        except InsufficientSnapshotHistory as error:
            print(f"{label}: not yet testable -- {error}")
        else:
            _report(result)
        print()

    print(
        "Fund Evidence: excluded by design -- it has no ordinal rating/score "
        "field to correlate against a forward return (see this module's own "
        "docstring)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
