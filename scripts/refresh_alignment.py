#!/usr/bin/env python
"""Explicitly recompute the Phase 2B Donatien <-> Market Regime alignment.

Unlike scripts/refresh_macro_regime.py and
scripts/refresh_donatien_calibration.py, this makes no network call and
uses no provider -- it only reads whatever AlphaLab Macro Regime and
Donatien External Calibration evidence is already stored, and computes a
purely categorical comparison (ALIGNED/CONFLICT/NEUTRAL/INSUFFICIENT_DATA,
never a numeric score). Run those two refreshes first if you want this to
reflect current evidence; running this alone just recomputes from whatever
is already in the database.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.alignment import AlignmentService  # noqa: E402
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402


def main() -> int:
    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)

    service = AlignmentService(engine)
    current = service.refresh()

    print("AlphaLab Alignment recomputed.")
    print(f"  scope: {current.scope}")
    print(f"  as_of: {current.as_of}")
    print(f"  alignment: {current.alignment}")
    print(f"  content_hash: {current.content_hash}")
    if current.alignment == "INSUFFICIENT_DATA":
        print("  Note: one or both of Market Regime / Donatien Calibration "
              "has not been refreshed yet -- run refresh_macro_regime.py "
              "and/or refresh_donatien_calibration.py first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
