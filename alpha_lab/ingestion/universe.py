"""Idempotent broad-universe metadata loading."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine

from alpha_lab.database.models import Security
from alpha_lab.database.session import session_scope
from alpha_lab.providers.interfaces import SecurityUniverseProvider


class UniverseIngestionService:
    def __init__(self, provider: SecurityUniverseProvider, engine: Engine):
        self.provider, self.engine = provider, engine

    def load(
        self,
        *,
        country: str = "US",
        exchanges: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> int:
        rows = self.provider.get_securities(country=country, exchanges=exchanges)
        if limit is not None:
            rows = rows[:limit]
        with session_scope(self.engine) as session:
            for raw in rows:
                values = _security_values(raw)
                existing = session.get(Security, values["ticker"])
                if existing is None:
                    session.add(Security(**values))
                else:
                    for key, value in values.items():
                        if value is not None or key in {
                            "metadata_provider",
                            "metadata_source",
                            "metadata_updated_at",
                        }:
                            setattr(existing, key, value)
        return len(rows)

    def enrich(self, metadata: list[dict[str, Any]]) -> int:
        with session_scope(self.engine) as session:
            count = 0
            for raw in metadata:
                ticker = str(raw["ticker"]).upper()
                existing = session.get(Security, ticker)
                if existing is None:
                    continue
                for key, value in _security_values(raw).items():
                    if key != "ticker" and value is not None:
                        setattr(existing, key, value)
                count += 1
        return count


def _security_values(raw: dict[str, Any]) -> dict[str, Any]:
    permitted = {
        "ticker",
        "company_name",
        "exchange",
        "country",
        "sector",
        "industry",
        "currency",
        "asset_type",
        "market_cap",
        "business_description",
        "metadata_provider",
        "metadata_source",
    }
    values = {key: raw.get(key) for key in permitted}
    values["ticker"] = str(values["ticker"]).upper().strip()
    values["metadata_updated_at"] = datetime.now(UTC)
    return values
