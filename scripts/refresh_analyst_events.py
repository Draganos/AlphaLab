#!/usr/bin/env python
"""Explicitly refresh analyst rating-change history (upgrades, downgrades,
initiations, reiterations) for US securities.

Each run fetches the source's ENTIRE rating-change history for a ticker
(see YFinanceProvider.get_analyst_rating_changes) and persists it via
alpha_lab.ingestion.analyst_events.snapshot_analyst_rating_changes --
content-hash-deduped, so already-known events are never duplicated and a
re-run with no new events is a no-op.

A ticker with no analyst coverage at all (confirmed live for ETFs:
yfinance returns an empty frame, not an error) stores zero events for that
ticker and is not treated as a failure. One ticker's provider failure
never aborts the rest of the run and never erases that ticker's
previously stored events.
"""
from dataclasses import dataclass, field
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.ingestion.analyst_events import snapshot_analyst_rating_changes  # noqa: E402
from alpha_lab.providers import ProviderError, YFinanceProvider  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402


@dataclass
class AnalystEventsRefreshResult:
    stored: dict[str, int] = field(default_factory=dict)
    empty: list[str] = field(default_factory=list)
    failed: dict[str, ProviderError] = field(default_factory=dict)

    @property
    def all_succeeded(self) -> bool:
        return not self.failed


def refresh_analyst_events(engine, provider, tickers) -> AnalystEventsRefreshResult:
    """Fetch + persist each ticker's full rating-change history. Never
    aborts the batch on one ticker's provider failure; never treats
    genuine "no coverage" (an empty result, not an exception) as a
    failure."""
    result = AnalystEventsRefreshResult()
    for ticker in tickers:
        try:
            events = provider.get_analyst_rating_changes(ticker)
        except ProviderError as error:
            result.failed[ticker] = error
            continue
        if not events:
            result.empty.append(ticker)
            continue
        inserted = snapshot_analyst_rating_changes(
            engine,
            ticker,
            events,
            provider=provider.provider_name,
            source="yfinance upgradeDowngradeHistory",
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
    result = refresh_analyst_events(engine, provider, tickers)

    print()
    print("Analyst rating-change refresh complete")
    print(f"Stored new events: {sum(result.stored.values())} across {len(result.stored)} ticker(s)")
    for ticker, count in result.stored.items():
        print(f"  {ticker}: {count} new event(s)")
    if result.empty:
        print(f"No analyst rating-change coverage (not a failure): {', '.join(result.empty)}")
    if result.failed:
        print(f"Failed: {len(result.failed)}")
        for ticker, error in result.failed.items():
            print(f"  {ticker}: {error.kind.value} - {error.reason}")
    return 0 if result.all_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
