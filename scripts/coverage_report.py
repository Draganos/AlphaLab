#!/usr/bin/env python
"""Report coverage from persisted current research without rebuilding it."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Callable, Sequence, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI options before any application or database setup occurs."""
    parser = argparse.ArgumentParser(
        description="Report coverage from the latest persisted current research build."
    )
    parser.add_argument(
        "--only-populated",
        action="store_true",
        help="include only records whose overall live coverage is greater than zero",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="suppress per-ticker output",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        metavar="TICKER",
        help="restrict the report and summary to these tickers",
    )
    return parser.parse_args(argv)


def normalize_tickers(tickers: Sequence[str] | None) -> list[str] | None:
    """Normalize requested symbols, retaining their first-seen order."""
    if tickers is None:
        return None
    return list(dict.fromkeys(ticker.strip().upper() for ticker in tickers))


def filter_records(records: Sequence, *, tickers=None, only_populated=False):
    """Return selected records and absent requested symbols without changing records."""
    requested = normalize_tickers(tickers)
    if requested is None:
        selected = list(records)
        missing: list[str] = []
    else:
        by_ticker = {record.ticker.upper(): record for record in records}
        selected = [by_ticker[ticker] for ticker in requested if ticker in by_ticker]
        missing = [ticker for ticker in requested if ticker not in by_ticker]
    if only_populated:
        selected = [record for record in selected if record.overall_live_coverage > 0]
    return selected, missing


def emit_report(
    records: Sequence,
    *,
    tickers: Sequence[str] | None = None,
    only_populated: bool = False,
    summary_only: bool = False,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> None:
    """Filter persisted records and emit strict, finite JSON lines."""
    from alpha_lab.ratings import summarize_coverage

    selected, missing = filter_records(
        records, tickers=tickers, only_populated=only_populated
    )
    if missing:
        print(f"Requested tickers not found: {', '.join(missing)}", file=stderr)
    if not summary_only:
        for record in selected:
            unavailable = [
                name for name, value in record.category_coverage.items() if value == 0
            ]
            print(
                json.dumps(
                    {
                        "ticker": record.ticker,
                        "overall_coverage": record.overall_live_coverage,
                        "category_coverage": record.category_coverage,
                        "unavailable_categories": unavailable,
                    },
                    sort_keys=True,
                    allow_nan=False,
                ),
                file=stdout,
            )
    print(
        json.dumps(asdict(summarize_coverage(selected)), sort_keys=True, allow_nan=False),
        file=stdout,
    )


def _read_persisted_records():
    """Read the latest snapshot; this deliberately exposes no rebuild path."""
    from alpha_lab.config import load_settings
    from alpha_lab.database import make_engine
    from alpha_lab.screener import MarketScreenerService

    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        return MarketScreenerService(engine, settings).read_current_research()
    finally:
        engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    load_records: Callable[[], Sequence] = _read_persisted_records,
) -> None:
    args = parse_args(argv)
    emit_report(
        load_records(),
        tickers=args.tickers,
        only_populated=args.only_populated,
        summary_only=args.summary_only,
    )


if __name__ == "__main__":
    main()
