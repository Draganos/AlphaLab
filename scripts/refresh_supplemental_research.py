#!/usr/bin/env python
"""Explicitly refresh Analyst Consensus, Technical Summary, and AI Research
Rating for US securities.

Separate from `scripts/rebuild_research.py` (which only recomputes the
existing fundamental score from already-ingested data and makes no network
calls by default): Analyst Consensus needs a live yfinance call per ticker.
Technical Summary and AI Research Rating do not call any provider -- they
are computed purely from already-persisted data -- but are refreshed here
too so all three new domains are updated together in one explicit step.

One ticker's Analyst Consensus failing (rate limited, network unavailable,
etc.) never aborts the rest of the run and never erases that ticker's
previously computed data; Technical Summary and the AI assessment still
refresh for it from whatever evidence is currently available.
"""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.providers import ProviderError, YFinanceProvider  # noqa: E402
from alpha_lab.research import ResearchService  # noqa: E402
from alpha_lab.research.supplemental_service import SupplementalResearchService  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", help="US tickers; defaults to configured universe")
    parser.add_argument(
        "--skip-analyst",
        action="store_true",
        help="Skip Analyst Consensus entirely (no network calls at all this run).",
    )
    args = parser.parse_args()
    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)

    provider = YFinanceProvider()
    supplemental = SupplementalResearchService(engine)
    research_service = ResearchService(engine, settings)

    tickers = args.tickers or settings.universe["us"]
    succeeded: list[str] = []
    failed: dict[str, ProviderError] = {}

    for ticker in tickers:
        analyst = None
        try:
            if not args.skip_analyst:
                analyst = supplemental.refresh_analyst_consensus(ticker, provider)
        except ProviderError as error:
            failed[ticker] = error

        technical = supplemental.refresh_technical_summary(ticker)

        base_research = research_service.get_stock_research(ticker)
        if base_research is not None:
            supplemental.refresh_ai_research_assessment(
                ticker,
                base_research,
                analyst_consensus=analyst,
                technical_summary=technical,
            )
        if ticker not in failed:
            succeeded.append(ticker)

    print()
    print("Supplemental research refresh complete")
    print(f"Succeeded: {len(succeeded)}")
    print(f"Failed: {len(failed)}")
    for ticker, error in failed.items():
        print(f"  {ticker}: {error.kind.value} - {error.reason}")
    print(
        "Technical Summary and AI Research Rating were still refreshed for "
        "failed tickers from whatever evidence is currently available."
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
