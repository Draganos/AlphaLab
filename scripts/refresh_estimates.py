#!/usr/bin/env python
"""Explicitly refresh analyst consensus estimates (current/next fiscal-year
EPS and revenue) for US securities.

Persists one genuine observation per ticker per run via
`alpha_lab.ingestion.estimates.snapshot_estimates` -- content-hash-deduped,
so an unchanged consensus is a no-op. Estimate revision history
(eps_revision_7d/30d/90d etc., see alpha_lab.ratings.estimates) only ever
accumulates from repeated real runs over elapsed time; this script never
backfills or fabricates a historical observation.

A ticker with no analyst estimate coverage at all (confirmed live for ETFs:
yfinance returns an empty frame, not an error) stores zero observations for
that ticker and is not treated as a failure. One ticker's provider failure
(network, rate limit, unclassified) never aborts the rest of the run and
never erases that ticker's previously stored estimates.
"""
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.ingestion.estimates import snapshot_estimates  # noqa: E402
from alpha_lab.providers import ProviderError, YFinanceProvider  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402


@dataclass
class EstimatesRefreshResult:
    stored: dict[str, int] = field(default_factory=dict)
    empty: list[str] = field(default_factory=list)
    failed: dict[str, ProviderError] = field(default_factory=dict)

    @property
    def all_succeeded(self) -> bool:
        return not self.failed


def refresh_estimates(engine, provider, tickers, observation_date: date) -> EstimatesRefreshResult:
    """Fetch + persist one estimates snapshot per ticker. Never aborts the
    batch on one ticker's provider failure; never treats genuine "no
    coverage" (an empty result, not an exception) as a failure."""
    result = EstimatesRefreshResult()
    for ticker in tickers:
        try:
            observations = provider.get_estimates(ticker, observation_date)
        except ProviderError as error:
            result.failed[ticker] = error
            continue
        if not observations:
            result.empty.append(ticker)
            continue
        inserted = snapshot_estimates(
            engine,
            ticker,
            observation_date,
            observations,
            provider=provider.provider_name,
            source="yfinance earningsEstimate/revenueEstimate",
        )
        result.stored[ticker] = inserted
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", help="US tickers; defaults to configured universe")
    args = parser.parse_args()
    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)

    provider = YFinanceProvider()
    tickers = args.tickers or settings.universe["us"]
    result = refresh_estimates(engine, provider, tickers, date.today())

    print()
    print("Estimates refresh complete")
    print(f"Stored new observations: {sum(result.stored.values())} across {len(result.stored)} ticker(s)")
    for ticker, count in result.stored.items():
        print(f"  {ticker}: {count} new observation(s)")
    if result.empty:
        print(f"No analyst estimate coverage (not a failure): {', '.join(result.empty)}")
    if result.failed:
        print(f"Failed: {len(result.failed)}")
        for ticker, error in result.failed.items():
            print(f"  {ticker}: {error.kind.value} - {error.reason}")
    return 0 if result.all_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
