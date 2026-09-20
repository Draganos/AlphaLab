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

from datetime import UTC, date, datetime, timedelta
import hashlib

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.database.models import CompanyDocument
from alpha_lab.providers.interfaces import CompanyDocumentProvider

# A full annual filing cycle (one 10-K plus up to four 10-Qs) comfortably
# fits inside 400 days -- the same lookback_days convention already used
# by alpha_lab.macro.service.MacroRegimeService.refresh, chosen here for
# the same reason: a deliberately round, documented "recent enough to be
# current state" bound, not derived from calibrating the window itself.
DOCUMENT_ANALYSIS_WINDOW_DAYS = 400


def select_documents_for_analysis(
    documents: list[CompanyDocument], *, as_of: date
) -> list[CompanyDocument]:
    """The trailing window of `documents` that reflects a company's
    CURRENT state as of `as_of`, not its entire filing history since
    inception.

    Real finding, not a hypothetical: the Signal Predictive-Value
    calibration study (ARCHITECTURE.md's Document ingestion hardening +
    calibration phase) found `RuleBasedFinancialResearchProvider`'s
    `ai_rating` trending upward over time for NVDA as more filings
    accumulated -- because its per-dimension scores are a running
    phrase-count over the ENTIRE cumulative document set, and ordinary
    corporate boilerplate skews net-positive far more often than
    negative, so scores drift toward the +-2 cap and stay there
    regardless of what the most recent filing actually says.

    This is a document-SELECTION problem, not a provider-math problem --
    fixed once here, for every `AIResearchProvider` this project has or
    will have (not a rule-based-specific patch): an LLM handed a
    multi-decade filer's entire history would face the identical recency
    dilution, and pay for it in tokens besides. See
    `DOCUMENT_ANALYSIS_WINDOW_DAYS` for the bound.
    """
    cutoff = as_of - timedelta(days=DOCUMENT_ANALYSIS_WINDOW_DAYS)
    return [document for document in documents if cutoff < document.document_date <= as_of]


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
