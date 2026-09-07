#!/usr/bin/env python
"""Explicitly refresh the AlphaLab Macro Regime assessment.

Ingests fresh price history for a small fixed set of market-observable
proxy tickers (^VIX, ^TNX, ^IRX, DX-Y.NYB, CL=F, GC=F) via the existing
IngestionService/YFinanceProvider, then computes a deterministic regime
read purely from stored data. Market-derived proxies only -- never official
economic data, and never a scoring input; see alpha_lab.macro.regime's
module docstring.

A ticker that fails to ingest does not abort the refresh -- it is reported
as unavailable for that one indicator (reduced coverage), and any
previously-ingested price history for it is left untouched.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.macro import MacroRegimeService  # noqa: E402
from alpha_lab.providers import YFinanceProvider  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402


def main() -> int:
    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)

    service = MacroRegimeService(engine)
    current = service.refresh(YFinanceProvider())

    print("AlphaLab Macro Regime refreshed.")
    print(f"  scope: {current.scope}")
    print(f"  regime: {current.regime}  (score: {current.regime_score})")
    print(f"  confidence: {current.confidence}  coverage: {current.coverage}")
    print(f"  as_of: {current.as_of}  content_hash: {current.content_hash}")
    if current.coverage < 1.0:
        print("  Note: coverage is below 100% -- one or more proxy tickers "
              "were unavailable this refresh (see the indicator detail).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
