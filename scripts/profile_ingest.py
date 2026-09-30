#!/usr/bin/env python
"""Profile one real ingest of TICKER against the configured database.

Prints the database size and row counts, then runs exactly what Full Refresh
does for that ticker (`IngestionService.ingest`, which writes) with the
provider calls timed separately from everything else, and the functions the
remaining time goes to. Use it to attribute a slow Full Refresh to Yahoo,
to the database (size, indexes, a synced/scanned folder), or to something
else. It writes the same rows a Full Refresh would; re-running is harmless
(unchanged bars are never re-inserted).
"""
import argparse
import cProfile
from datetime import date, timedelta
from pathlib import Path
import pstats
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import make_engine  # noqa: E402
from alpha_lab.database.models import Price, Security  # noqa: E402
from alpha_lab.database.session import session_scope  # noqa: E402
from alpha_lab.ingestion import IngestionService  # noqa: E402
from alpha_lab.providers.yfinance_provider import YFinanceProvider  # noqa: E402


class _TimedProvider(YFinanceProvider):
    def __init__(self):
        self.seconds: dict[str, float] = {}

    def _timed(self, name, call):
        started = time.perf_counter()
        try:
            return call()
        finally:
            self.seconds[name] = self.seconds.get(name, 0.0) + time.perf_counter() - started

    def get_company_info(self, ticker):
        return self._timed("company info", lambda: super(_TimedProvider, self).get_company_info(ticker))

    def get_price_history(self, ticker, start, end):
        return self._timed("price history", lambda: super(_TimedProvider, self).get_price_history(ticker, start, end))

    def get_financials(self, ticker):
        return self._timed("financials", lambda: super(_TimedProvider, self).get_financials(ticker))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    args = parser.parse_args()
    symbol = args.ticker.upper().strip()
    settings = load_settings()
    engine = make_engine(settings.database_url)

    print(f"Database: {settings.database_url}")
    path = engine.url.database
    if path:
        print(f"  file size: {Path(path).stat().st_size / 1e6:,.0f} MB")
    with session_scope(engine) as session:
        print(f"  securities: {session.scalar(select(func.count()).select_from(Security)):,}")
        print(f"  price rows: {session.scalar(select(func.count()).select_from(Price)):,}")
        print(f"  price rows for {symbol}: {session.scalar(select(func.count()).select_from(Price).where(Price.ticker == symbol)):,}")
    with engine.connect() as connection:
        indexes = [row[1] for row in connection.exec_driver_sql("PRAGMA index_list('prices')")]
    print(f"  prices indexes: {', '.join(indexes) or 'none'}")

    provider = _TimedProvider()
    end = date.today()
    start = end - timedelta(days=365 * 2)
    profiler = cProfile.Profile()
    started = time.perf_counter()
    profiler.enable()
    IngestionService(provider, engine).ingest(symbol, start, end)
    profiler.disable()
    total = time.perf_counter() - started

    network = sum(provider.seconds.values())
    print(f"\nIngest of {symbol}: {total:.1f}s total")
    for name, seconds in provider.seconds.items():
        print(f"  {name:<14}{seconds:6.1f}s")
    print(f"  everything else (database/CPU): {total - network:.1f}s")
    print("\nTop functions by cumulative time:")
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(12)
    return 0


if __name__ == "__main__":
    sys.exit(main())
