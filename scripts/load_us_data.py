#!/usr/bin/env python
"""Download real provider data for configured US securities.

One ticker failing (rate limited, network unavailable, or otherwise) never
aborts the run for the rest of the universe, and never counts as success:
each ticker's outcome is tracked and reported, and the process exits
non-zero if any requested ticker did not complete -- so scheduled/automated
ingestion can detect an incomplete load instead of silently reporting green.
"""
from datetime import date, timedelta
from pathlib import Path
import argparse
import logging
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers import ProviderError, YFinanceProvider
from alpha_lab.utils.logging import configure_logging

logger = logging.getLogger(__name__)


def _ingest_universe(service: IngestionService, tickers: list[str], years: int) -> bool:
    """Ingest every ticker, continuing past individual failures.

    Returns True only if every ticker succeeded.
    """
    start = date.today() - timedelta(days=365 * years)
    end = date.today()
    succeeded: list[str] = []
    failed: dict[str, ProviderError] = {}
    for ticker in tickers:
        try:
            service.ingest(ticker, start, end)
        except ProviderError as error:
            failed[ticker] = error
            logger.error(
                "ticker_ingestion_failed",
                extra={"ticker": ticker, "kind": error.kind.value, "reason": error.reason},
            )
        else:
            succeeded.append(ticker)

    _print_summary(succeeded, failed)
    return not failed


def _print_summary(succeeded: list[str], failed: dict[str, ProviderError]) -> None:
    by_kind: dict[str, list[str]] = {}
    for ticker, error in failed.items():
        by_kind.setdefault(error.kind.value, []).append(ticker)

    print()
    print("US ingestion complete")
    print(f"Succeeded: {len(succeeded)}")
    print(f"Failed: {len(failed)}")
    for kind, tickers in sorted(by_kind.items()):
        label = kind.replace("_", " ").title()
        detail = ", ".join(f"{t} ({failed[t].reason})" for t in tickers)
        print(f"  {label}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tickers", nargs="*", help="US tickers; defaults to configured universe")
    parser.add_argument("--years", type=int, default=5)
    args = parser.parse_args()
    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)
    service = IngestionService(YFinanceProvider(), engine)
    tickers = args.tickers or settings.universe["us"]

    if len(tickers) == 1:
        ticker = tickers[0]
        try:
            service.ingest(
                ticker,
                date.today() - timedelta(days=365 * args.years),
                date.today(),
            )
        except ProviderError as error:
            print(f"ERROR: {ticker} ingestion failed")
            print(f"Provider: {error.provider}")
            print(f"Reason: {error.kind.value} - {error.reason}")
            logger.error(
                "ticker_ingestion_failed",
                extra={"ticker": ticker, "kind": error.kind.value, "reason": error.reason},
                exc_info=error,
            )
            return 1
        print("US ingestion complete\nSucceeded: 1\nFailed: 0")
        return 0

    all_succeeded = _ingest_universe(service, tickers, args.years)
    return 0 if all_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
