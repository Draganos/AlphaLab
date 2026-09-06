#!/usr/bin/env python
"""Report the yfinance/curl_cffi/Python environment and, optionally, how a
single live call against Yahoo Finance currently classifies.

Safe to run with no arguments: it only reports installed versions, never
touches the network. Pass --ticker to make one live get_company_info() call
and print how AlphaLab's error boundary classifies the outcome (success, or
which ProviderErrorKind). Never used by the automated test suite.
"""
import argparse
from pathlib import Path
import platform
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yfinance  # noqa: E402

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
        return 0


if __name__ == "__main__":
    sys.exit(main())
