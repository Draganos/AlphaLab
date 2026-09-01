#!/usr/bin/env python
"""Emit stored evidence behind current-research coverage for selected tickers."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import make_engine  # noqa: E402
from alpha_lab.database.models import Fundamental, Price, Security  # noqa: E402
from alpha_lab.phase3 import Phase3Repository  # noqa: E402
from alpha_lab.screener import LiveResearchRecord  # noqa: E402
from alpha_lab.screener.service import CATEGORY_EVIDENCE_METRICS  # noqa: E402


DIAGNOSTIC_CATEGORIES = (
    "momentum",
    "valuation",
    "shareholder_return",
    "financial_strength",
)


def diagnose(engine, tickers: list[str]) -> list[dict]:
    """Return current inputs and provenance without rebuilding or imputing data."""
    _, payloads = Phase3Repository(engine).latest_current_payloads()
    records = {
        item.ticker: item
        for item in (LiveResearchRecord.model_validate(payload) for payload in payloads)
    }
    output = []
    with Session(engine) as session:
        for ticker in tickers:
            security = session.get(Security, ticker)
            price_summary = session.execute(
                select(
                    func.count(Price.id),
                    func.min(Price.date),
                    func.max(Price.date),
                ).where(Price.ticker == ticker)
            ).one()
            price_providers = Counter(
                session.scalars(select(Price.provider).where(Price.ticker == ticker))
            )
            fundamental_providers = Counter(
                session.scalars(
                    select(Fundamental.provider).where(Fundamental.ticker == ticker)
                )
            )
            record = records.get(ticker)
            output.append(
                {
                    "ticker": ticker,
                    "security": _security_evidence(security),
                    "stored_prices": {
                        "count": price_summary[0],
                        "minimum_date": _iso(price_summary[1]),
                        "maximum_date": _iso(price_summary[2]),
                        "providers": dict(sorted(price_providers.items())),
                    },
                    "stored_fundamental_providers": dict(
                        sorted(fundamental_providers.items())
                    ),
                    "current_research": _research_evidence(record),
                }
            )
    return output


def _research_evidence(record: LiveResearchRecord | None) -> dict | None:
    if record is None:
        return None
    metrics = {
        category: {
            name: record.raw_metrics.get(name)
            for name in CATEGORY_EVIDENCE_METRICS[category]
        }
        for category in DIAGNOSTIC_CATEGORIES
    }
    return {
        "evaluation_date": record.evaluation_date.isoformat(),
        "latest_valid_price": record.price,
        "data_quality_status": record.data_quality_status,
        "category_coverage": {
            name: record.category_coverage[name] for name in DIAGNOSTIC_CATEGORIES
        },
        "overall_live_coverage": record.overall_live_coverage,
        "raw_metrics": metrics,
        "provenance": record.provenance,
    }


def _security_evidence(security: Security | None) -> dict | None:
    if security is None:
        return None
    return {
        "company_name": security.company_name,
        "exchange": security.exchange,
        "market_cap": security.market_cap,
        "metadata_provider": security.metadata_provider,
        "metadata_source": security.metadata_source,
        "metadata_updated_at": _iso(security.metadata_updated_at),
    }


def _iso(value):
    return value.isoformat() if value is not None else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnose stored inputs behind current coverage; never rebuilds data"
    )
    parser.add_argument("tickers", nargs="+", type=lambda value: value.strip().upper())
    args = parser.parse_args()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    try:
        print(json.dumps(diagnose(engine, list(dict.fromkeys(args.tickers))), sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
