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


def _content_hash(ticker: str, *, source: str | None, text: str) -> str:
    """Identifies one distinct filing/document event, not merely one block
    of text. Prefers `source` -- for `SECFilingDocumentProvider`, a URL
    that already uniquely encodes the filing's own CIK, accession number,
    and primary document (SEC's own unique submission identifiers) -- over
    raw text: two genuinely distinct filings (e.g. a 10-K and a later
    10-K/A amendment, or two exhibits sharing boilerplate) can carry
    byte-identical extracted text but are still two separate information
    events, and must never collide into a single stored row just because
    their text happens to match. Falls back to `(ticker, text)` only when
    a provider supplies no source URL at all (not true for
    `SECFilingDocumentProvider`, which always does)."""
    identity = f"{ticker}\x00{source}" if source else f"{ticker}\x00{text}"
    return hashlib.sha256(identity.encode()).hexdigest()


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
    content_hashes = [
        _content_hash(normalized, source=raw.get("source"), text=raw["text"])
        for raw in raw_documents
    ]
    with Session(engine) as session:
        # One batched lookup for every candidate hash rather than one
        # SELECT per document -- a ticker with dozens of already-ingested
        # filings would otherwise pay one round trip per filing on every
        # re-run just to discover it has nothing new to store.
        existing_hashes = set(
            session.scalars(
                select(CompanyDocument.content_hash).where(
                    CompanyDocument.content_hash.in_(content_hashes)
                )
            )
        )
        for raw, content_hash in zip(raw_documents, content_hashes, strict=True):
            if content_hash in existing_hashes:
                continue
            existing_hashes.add(content_hash)  # guards against duplicate rows within this same batch
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
