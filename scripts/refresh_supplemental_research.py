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
previously computed data. Technical Summary still refreshes for it from
already-stored data, but AI Research Rating is skipped for that ticker --
refreshing it without an Analyst Consensus domain would silently replace a
previously valid assessment with a weaker one derived from incomplete
evidence, rather than surfacing the failure. See
SupplementalResearchService.refresh_all.
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
        base_research = research_service.get_stock_research(ticker)

        if args.skip_analyst:
            # Deliberately omitting Analyst Consensus for the whole run is
            # not a failure -- AI Research Rating still refreshes, honestly
            # missing that one domain, same as before this fix.
            technical = supplemental.refresh_technical_summary(ticker)
            if base_research is not None:
                supplemental.refresh_ai_research_assessment(
                    ticker, base_research, analyst_consensus=None, technical_summary=technical
                )
            succeeded.append(ticker)
            continue

        if base_research is None:
            # No fundamental research yet for this ticker -- nothing to
            # synthesize an AI assessment from, but Analyst Consensus and
            # Technical Summary still refresh independently.
            try:
                supplemental.refresh_analyst_consensus(ticker, provider)
            except ProviderError as error:
                failed[ticker] = error
            supplemental.refresh_technical_summary(ticker)
            if ticker not in failed:
                succeeded.append(ticker)
            continue

        result = supplemental.refresh_all(ticker, provider, base_research)
        if result.analyst_error is not None:
            failed[ticker] = result.analyst_error
        else:
            succeeded.append(ticker)

    print()
    print("Supplemental research refresh complete")
    print(f"Succeeded: {len(succeeded)}")
    print(f"Failed: {len(failed)}")
    for ticker, error in failed.items():
        print(f"  {ticker}: {error.kind.value} - {error.reason}")
    if failed:
        print(
            "Technical Summary was still refreshed for failed tickers. AI "
            "Research Rating was skipped for them -- refreshing it without "
            "Analyst Consensus would have silently degraded a previously "
            "valid assessment."
        )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
