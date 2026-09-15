"""Idempotent estimate-revision-trend snapshot persistence.

Mirrors ``alpha_lab.ingestion.estimates.snapshot_estimates``'s content-hash
dedup pattern exactly, for ``EstimateRevisionTrend`` rows (the source's own
reported EPS trend/revision-count history, see that model's docstring for
how it differs from ``Estimate``).
"""

from datetime import date
import hashlib
import json
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import EstimateRevisionTrend


def snapshot_estimate_revisions(
    engine: Engine,
    ticker: str,
    observation_date: date,
    observations: list[dict[str, Any]],
    *,
    provider: str,
    source: str | None = None,
) -> int:
    """Append distinct observations and return the number inserted."""
    inserted = 0
    with Session(engine) as session:
        for value in observations:
            record_values = {
                "ticker": ticker.upper(),
                "observation_date": observation_date,
                "fiscal_period": value["fiscal_period"],
                "eps_trend_current": value.get("eps_trend_current"),
                "eps_trend_7d_ago": value.get("eps_trend_7d_ago"),
                "eps_trend_30d_ago": value.get("eps_trend_30d_ago"),
                "eps_trend_60d_ago": value.get("eps_trend_60d_ago"),
                "eps_trend_90d_ago": value.get("eps_trend_90d_ago"),
                "revisions_up_last_7d": value.get("revisions_up_last_7d"),
                "revisions_up_last_30d": value.get("revisions_up_last_30d"),
                "revisions_down_last_7d": value.get("revisions_down_last_7d"),
                "revisions_down_last_30d": value.get("revisions_down_last_30d"),
                "provider": provider,
                "source": source,
                "currency": value.get("currency"),
            }
            payload = {
                key: item.isoformat() if isinstance(item, date) else item
                for key, item in record_values.items()
            }
            observation_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if session.scalar(
                select(EstimateRevisionTrend.id).where(
                    EstimateRevisionTrend.observation_hash == observation_hash
                )
            ):
                continue
            session.add(
                EstimateRevisionTrend(**record_values, observation_hash=observation_hash)
            )
            inserted += 1
        session.commit()
    return inserted
