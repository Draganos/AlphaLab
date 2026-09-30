#!/usr/bin/env python
"""Report the yfinance/curl_cffi/Python environment and, optionally, how a
single live call against Yahoo Finance currently classifies.

Safe to run with no arguments: it only reports installed versions, never
touches the network. Pass --ticker to make one live get_company_info() call
and print how AlphaLab's error boundary classifies the outcome (success, or
which ProviderErrorKind), then time each of the three calls a real ingest
makes (company info, price history, financials) and a full ingest into a
throwaway in-memory database, so a slow Full Refresh can be attributed to the
network/Yahoo or to local processing. Never used by the automated test suite.
"""
import argparse
from datetime import date, timedelta
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yfinance  # noqa: E402

from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.ingestion import IngestionService  # noqa: E402
from alpha_lab.providers.errors import ProviderError  # noqa: E402
from alpha_lab.providers.yfinance_provider import YFinanceProvider  # noqa: E402


def _http_backend_name() -> str:
    try:
        from yfinance._http import HAS_CURL_CFFI

        return "curl_cffi" if HAS_CURL_CFFI else "requests (fallback)"
    except ImportError:
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ticker",
        help="Make one live get_company_info() call against this ticker and report classification.",
    )
    args = parser.parse_args()

    print(f"Python: {platform.python_version()}")
    print(f"yfinance: {yfinance.__version__}")
    try:
        import curl_cffi

        print(f"curl_cffi: {curl_cffi.__version__}")
    except ImportError as error:
        print(f"curl_cffi: not importable (absent, or blocked by OS policy: {error})")
    print(f"HTTP backend in use: {_http_backend_name()}")

    if not args.ticker:
        print("\nPass --ticker SYMBOL to make one live diagnostic call.")
        return 0

    print(f"\nMaking one live get_company_info() call for {args.ticker}...")
    provider = YFinanceProvider()
    try:
        info = provider.get_company_info(args.ticker)
    except ProviderError as error:
        print(f"Classified as: {error.kind.value}")
        print(f"Reason: {error.reason}")
        print(f"Underlying exception: {type(error.__cause__).__name__}: {error.__cause__}")
        return 1
    else:
        print("Success.")
        print(f"company_name={info.get('company_name')!r} exchange={info.get('exchange')!r}")
    return _time_ingest_steps(provider, args.ticker)


def _timed(label: str, call) -> bool:
    started = time.perf_counter()
    try:
        call()
    except ProviderError as error:
        print(f"  {label:<28}{time.perf_counter() - started:6.1f}s  FAILED: {error.kind.value} ({error.reason})")
        return False
    print(f"  {label:<28}{time.perf_counter() - started:6.1f}s  ok")
    return True


def _time_ingest_steps(provider: YFinanceProvider, ticker: str) -> int:
    end = date.today()
    start = end - timedelta(days=365 * 2)
    print(f"\nTiming each call a real ingest of {ticker} makes:")
    ok = _timed("company info", lambda: provider.get_company_info(ticker))
    ok &= _timed("price history (2y)", lambda: provider.get_price_history(ticker, start, end))
    ok &= _timed("financials (3 statements)", lambda: provider.get_financials(ticker))
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    ok &= _timed("full ingest (empty DB)", lambda: IngestionService(provider, engine).ingest(ticker, start, end))
    print(
        "\nRule of thumb: each step ~0.3-1.5s is normal. Steps of 10s+ or FAILED rows mean "
        "Yahoo/network throttling (wait and retry, or change network); fast steps here but a "
        "slow Full Refresh mean the time is local."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
