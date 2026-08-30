#!/usr/bin/env python
"""Report metric-level coverage from the latest persisted current research build."""

from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import make_engine  # noqa: E402
from alpha_lab.ratings import summarize_coverage  # noqa: E402
from alpha_lab.screener import MarketScreenerService  # noqa: E402


def main() -> None:
    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        records = MarketScreenerService(engine, settings).read_current_research()
        for record in records:
            unavailable = [name for name, value in record.category_coverage.items() if value == 0]
            print(json.dumps({
                "ticker": record.ticker,
                "overall_coverage": record.overall_live_coverage,
                "category_coverage": record.category_coverage,
                "unavailable_categories": unavailable,
            }, sort_keys=True))
        print(json.dumps(summarize_coverage(records).__dict__, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
