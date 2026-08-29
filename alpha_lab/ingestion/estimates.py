"""Idempotent estimate snapshot persistence for genuine point-in-time revision history."""

from datetime import date
import hashlib
import json
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import Estimate


def snapshot_estimates(
    engine: Engine,
    ticker: str,
    observation_date: date,
    observations: list[dict[str, Any]],
    *,
    provider: str,
    source: str | None = None,
    currency: str | None = None,
) -> int:
    """Append distinct observations and return the number inserted."""
    inserted = 0
    with Session(engine) as session:
        for value in observations:
            record_values = {
                "ticker": ticker.upper(),
                "observation_date": observation_date,
                "fiscal_period": value["fiscal_period"],
                "consensus_eps": value.get("consensus_eps"),
                "consensus_revenue": value.get("consensus_revenue"),
                "analyst_count": value.get("analyst_count"),
                "estimate_dispersion": value.get("estimate_dispersion"),
                "provider": provider,
                "source": source,
                "currency": currency,
            }
            payload = {
                key: item.isoformat() if isinstance(item, date) else item
                for key, item in record_values.items()
            }
            observation_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if session.scalar(
                select(Estimate.id).where(Estimate.observation_hash == observation_hash)
            ):
                continue
            session.add(Estimate(**record_values, observation_hash=observation_hash))
            inserted += 1
        session.commit()
    return inserted
