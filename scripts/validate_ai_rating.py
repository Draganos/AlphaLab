#!/usr/bin/env python
"""Run the frozen AI Rating validation protocol (roadmap Phase 7) against
the persisted ResearchSnapshot history. Read-only: no network, no writes,
never wired into any scoring/ranking path. See
alpha_lab.analytics.ai_rating_validation's module docstring for the
protocol, its criteria, and why the expected outcome while history is
sparse is NOT_TESTABLE / INSUFFICIENT_HISTORY rather than a number."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.analytics.ai_rating_validation import (  # noqa: E402
    AIRatingValidationReport,
    validate_ai_rating,
)
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import make_engine  # noqa: E402
from alpha_lab.refresh import configured_universe_tickers  # noqa: E402


def _print_report(report: AIRatingValidationReport) -> None:
    m = report.methodology
    print(f"Methodology: {m.methodology_version} | provider={m.provider} | model={m.model} | prompt={m.prompt_version}")
    print(f"  protocol: {report.protocol_version}")
    print(f"  scored snapshots: {report.scored_snapshots} (REVIEW/unscored excluded: {report.review_excluded})")
    for horizon in report.horizons:
        line = (
            f"  horizon {horizon.horizon_days:>2}d: {horizon.independent_observations} independent obs, "
            f"{horizon.distinct_tickers} tickers"
        )
        if not horizon.testable:
            print(line + " -- below sample floor, no statistics reported")
            continue
        print(line)
        print(f"    pearson {horizon.pearson:+.3f} | spearman {horizon.spearman:+.3f}")
        if horizon.deterministic_spearman is not None:
            print(f"    deterministic-score spearman {horizon.deterministic_spearman:+.3f}")
        if horizon.incremental is not None:
            inc = horizon.incremental
            print(f"    incremental AI beta {inc.ai_beta:+.4f} (t={inc.ai_t_stat:+.2f}, n={inc.n})")
        split = horizon.confidence_split
        if split is not None:
            print(f"    confidence split: high n={split.high_n}, low n={split.low_n}, {split.result.value}")
    for criterion in report.criteria:
        print(f"  {criterion.name}: {criterion.result.value} -- {criterion.detail}")
    print(f"  STATUS: {report.status.value}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", help="Tickers to analyze; defaults to the configured universe")
    args = parser.parse_args()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    tickers = args.tickers or configured_universe_tickers(engine)
    reports = validate_ai_rating(engine, settings, tickers)
    if not reports:
        print("STATUS: NOT_TESTABLE -- no ResearchSnapshot in the requested tickers carries an AI Research Rating.")
        return 0
    for report in reports:
        _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
