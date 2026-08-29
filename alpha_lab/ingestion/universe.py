"""Idempotent broad-universe metadata loading."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

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

    def research_tickers(
        self, *, limit: int | None, exchanges: tuple[str, ...] = ("NASDAQ", "NYSE")
    ) -> list[str]:
        """Select an exchange-balanced subset; market cap orders each exchange when known."""
        with Session(self.engine) as session:
            securities = list(
                session.scalars(
                    select(Security).where(Security.exchange.in_(exchanges))
                )
            )
        groups: dict[str, list[Security]] = {exchange: [] for exchange in exchanges}
        for security in securities:
            groups.setdefault(security.exchange or "Unknown", []).append(security)
        for values in groups.values():
            values.sort(
                key=lambda item: (
                    item.market_cap is None,
                    -(item.market_cap or 0),
                    item.ticker,
                )
            )
        ordered: list[str] = []
        positions = {exchange: 0 for exchange in exchanges}
        while any(
            positions[exchange] < len(groups.get(exchange, []))
            for exchange in exchanges
        ):
            for exchange in exchanges:
                position = positions[exchange]
                values = groups.get(exchange, [])
                if position < len(values):
                    ordered.append(values[position].ticker)
                    positions[exchange] += 1
                    if limit is not None and len(ordered) >= limit:
                        return ordered
        return ordered

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
                        if key == "exchange" and existing.exchange in {
                            "NASDAQ",
                            "NYSE",
                        }:
                            continue
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
    values["exchange"] = _canonical_exchange(values.get("exchange"))
    values["metadata_updated_at"] = datetime.now(UTC)
    return values


def _canonical_exchange(value: Any) -> str | None:
    if value is None:
        return None
    label = str(value).upper().strip()
    return {
        "NMS": "NASDAQ",
        "NGM": "NASDAQ",
        "NCM": "NASDAQ",
        "NAS": "NASDAQ",
        "NYQ": "NYSE",
        "NYE": "NYSE",
    }.get(label, label)
