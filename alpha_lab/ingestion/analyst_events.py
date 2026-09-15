"""Idempotent analyst rating-change persistence.

Mirrors ``alpha_lab.ingestion.estimates.snapshot_estimates``'s content-hash
dedup pattern, but for a source that returns its ENTIRE event history on
every call (see ``YFinanceProvider.get_analyst_rating_changes``) rather than
only new observations -- so a full re-fetch on every refresh is expected
and correct, and content-hash dedup is what keeps repeated refreshes from
duplicating already-known events.
"""

from datetime import date, datetime
import hashlib
import json
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import AnalystRatingChange


def snapshot_analyst_rating_changes(
    engine: Engine,
    ticker: str,
    events: list[dict[str, Any]],
    *,
    provider: str,
    source: str | None = None,
) -> int:
    """Append distinct rating-change events and return the number inserted."""
    inserted = 0
    with Session(engine) as session:
        for event in events:
            record_values = {
                "ticker": ticker.upper(),
                "grade_date": event["grade_date"],
                "firm": event.get("firm"),
                "to_grade": event.get("to_grade"),
                "from_grade": event.get("from_grade"),
                "action": event.get("action"),
                "price_target_action": event.get("price_target_action"),
                "current_price_target": event.get("current_price_target"),
                "prior_price_target": event.get("prior_price_target"),
                "provider": provider,
                "source": source,
            }
            payload = {
                key: item.isoformat() if isinstance(item, (date, datetime)) else item
                for key, item in record_values.items()
            }
            content_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if session.scalar(
                select(AnalystRatingChange.id).where(
                    AnalystRatingChange.content_hash == content_hash
                )
            ):
                continue
            session.add(AnalystRatingChange(**record_values, content_hash=content_hash))
            inserted += 1
        session.commit()
    return inserted
