#!/usr/bin/env python
"""Record the Conviction/Conflict Layer's assessment for each tracked
security into the immutable `conviction_assessments` audit table (roadmap
Phase 8). Read-only against research: it assembles each ticker's
ResearchState exactly as the dashboard does (no network, no provider, no AI),
assesses it, and appends the result. Re-running with unchanged evidence adds
nothing. `--dry-run` assesses and prints without writing.

The audit table is evidence for a later Phase 7-style validation, never a
second source of truth: nothing reads it back into live research.
"""

import argparse
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.conviction import assess_conviction, record_assessment  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.refresh import configured_universe_tickers  # noqa: E402
from alpha_lab.research_state import get_research_state  # noqa: E402
from alpha_lab.strategy import HistoricalScoringService  # noqa: E402


def assess_universe(engine, settings, tickers: list[str], *, record: bool = True) -> list[dict]:
    """One row per ticker: its assessment and whether it was newly recorded."""
    tickers = [ticker.strip().upper() for ticker in tickers]
    today = date.today()
    # Scored once for the whole set (percentile ranking needs the universe),
    # so the loop stays O(N) rather than re-scoring per ticker.
    universe_scores = HistoricalScoringService(engine, settings).score_universe_as_of(
        today, tickers=sorted(set(configured_universe_tickers(engine)) | set(tickers)),
        min_score=0, minimum_coverage=0,
    )
    stale_price_days = settings.data_quality["stale_price_days"]
    rows = []
    for ticker in tickers:
        state = get_research_state(engine, settings, ticker, universe_scores=universe_scores)
        assessment = assess_conviction(state, coverage=settings.coverage, stale_price_days=stale_price_days)
        recorded = (
            record_assessment(engine, state, assessment, coverage=settings.coverage, stale_price_days=stale_price_days)
            if record else False
        )
        rows.append({"ticker": ticker, "assessment": assessment, "recorded": recorded})
    return rows


def _format(row: dict) -> str:
    assessment = row["assessment"]
    families = " | ".join(
        f"{family.family.value[:4]}:{family.lean.value[:3]}/{family.status.value[:4]}"
        for family in assessment.families
    )
    hard = sum(1 for conflict in assessment.conflicts if conflict.strength.value == "HARD")
    return (
        f"{row['ticker']:<8}{assessment.agreement.value:<13}{assessment.direction.value:<9}"
        f"{families}  conflicts={len(assessment.conflicts)} (hard {hard})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", help="Tickers to assess; defaults to the tracked research universe")
    parser.add_argument("--dry-run", action="store_true", help="Assess and print; write nothing")
    args = parser.parse_args()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)
    tickers = args.tickers or configured_universe_tickers(engine)
    if not tickers:
        print("No tracked securities to assess. Use `python scripts/manage_universe.py add TICKER ...`.")
        return 0
    rows = assess_universe(engine, settings, tickers, record=not args.dry_run)
    for row in rows:
        print(_format(row))
    if args.dry_run:
        print(f"\nDry run: {len(rows)} assessment(s) computed, nothing written.")
    else:
        new = sum(1 for row in rows if row["recorded"])
        print(f"\nRecorded {new} new assessment(s); {len(rows) - new} unchanged (already on file).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
