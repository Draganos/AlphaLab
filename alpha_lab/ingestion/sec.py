"""Append-only persistence for official SEC facts and filing-version fundamentals."""

from datetime import UTC, datetime
import hashlib
import json

from sqlalchemy import Engine, select

from alpha_lab.database.models import Fundamental, SECCompanyFact, Security
from alpha_lab.database.session import session_scope
from alpha_lab.providers.sec_edgar import SECCompanyFactsProvider, select_filing_metrics


class SECIngestionService:
    def __init__(self, provider: SECCompanyFactsProvider, engine: Engine):
        self.provider, self.engine = provider, engine

    def ingest(self, ticker: str, cik: str) -> tuple[int, int]:
        facts = self.provider.get_facts(ticker, cik)
        snapshots = select_filing_metrics(facts)
        raw_added = fundamentals_added = 0
        with session_scope(self.engine) as session:
            if session.get(Security, ticker) is None:
                session.add(Security(ticker=ticker))
                session.flush()
            for fact in facts:
                exists = session.scalar(
                    select(SECCompanyFact.id).where(
                        SECCompanyFact.ticker == fact.ticker,
                        SECCompanyFact.accession == fact.accession,
                        SECCompanyFact.concept == fact.concept,
                        SECCompanyFact.unit == fact.unit,
                        SECCompanyFact.period_start == fact.period_start,
                        SECCompanyFact.period_end == fact.period_end,
                    )
                )
                if exists is None:
                    session.add(SECCompanyFact(**fact.__dict__))
                    raw_added += 1
            for snapshot in snapshots:
                values = snapshot["values"]
                identity = {
                    "ticker": ticker,
                    "accession": snapshot["accession"],
                    "period": snapshot["period"].isoformat(),
                    "filed": snapshot["publication_date"].isoformat(),
                    "values": values,
                }
                observation_hash = hashlib.sha256(
                    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if session.scalar(
                    select(Fundamental.id).where(
                        Fundamental.observation_hash == observation_hash
                    )
                ) is None:
                    session.add(Fundamental(
                        ticker=ticker,
                        period=snapshot["period"],
                        publication_date=snapshot["publication_date"],
                        provider=self.provider.provider_name,
                        source=snapshot["source"],
                        currency="USD",
                        observation_hash=observation_hash,
                        ingested_at=datetime.now(UTC),
                        provenance_json={
                            "accession": snapshot["accession"],
                            "form": snapshot["form"],
                            "fiscal_period": snapshot["fiscal_period"],
                            "metrics": snapshot["provenance"],
                        },
                        **values,
                    ))
                    fundamentals_added += 1
        return raw_added, fundamentals_added
