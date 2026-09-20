"""Persistence for `CompanyDocumentProvider` output -- the ingestion half
`alpha_lab.ai.service.AIResearchService.ensure_all` was always missing (it
only ever read `CompanyDocument` rows, nothing wrote them). See
ARCHITECTURE.md's Document Evidence Engine phase.

Append-only, content-hash deduplicated, exactly like `NewsService.refresh`:
the provider call happens entirely before any database write, so a failed
ingestion attempt leaves previously stored documents completely untouched,
and re-ingesting a ticker whose filings haven't changed is a no-op rather
than a growing pile of duplicates.
"""

from datetime import UTC, date, datetime
import hashlib

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import CompanyDocument
from alpha_lab.providers.interfaces import CompanyDocumentProvider


def _content_hash(ticker: str, text: str) -> str:
    """Identifies an exact (ticker, document text) pair -- if SEC ever
    re-serves the identical filing text, this is what makes a re-run a
    no-op instead of a duplicate row."""
    payload = f"{ticker}\x00{text}".encode()
    return hashlib.sha256(payload).hexdigest()


def ingest_company_documents(
    engine: Engine, provider: CompanyDocumentProvider, ticker: str, *, since: date | None = None
) -> int:
    """Fetch `ticker`'s documents from `provider` and persist any not
    already stored (by content hash). Returns the count of newly stored
    documents -- 0 is a legitimate, honest outcome (nothing new, or the
    provider genuinely has nothing for this ticker), never an error."""
    normalized = ticker.strip().upper()
    raw_documents = provider.get_documents(normalized, since=since)  # network call before any write
    if not raw_documents:
        return 0
    retrieved_at = datetime.now(UTC).replace(tzinfo=None)
    stored = 0
    with Session(engine) as session:
        for raw in raw_documents:
            content_hash = _content_hash(normalized, raw["text"])
            existing = session.scalar(
                select(CompanyDocument.id).where(CompanyDocument.content_hash == content_hash)
            )
            if existing is not None:
                continue
            session.add(CompanyDocument(
                ticker=normalized,
                document_date=raw["document_date"],
                document_type=raw["document_type"],
                title=raw["title"],
                text=raw["text"],
                source=raw.get("source"),
                content_hash=content_hash,
                retrieved_at=retrieved_at,
            ))
            stored += 1
        session.commit()
    return stored
