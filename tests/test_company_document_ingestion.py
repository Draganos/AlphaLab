"""Offline tests for alpha_lab.ai.documents.ingest_company_documents: the
persistence half of the Document Evidence Engine that
AIResearchService.ensure_all always needed but never had."""

from datetime import date

import pytest

from alpha_lab.ai.documents import ingest_company_documents
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import CompanyDocument, Security
from sqlalchemy import select
from sqlalchemy.orm import Session


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="NVDA", country="US", currency="USD"))
        session.commit()
    try:
        yield engine
    finally:
        engine.dispose()


class _FakeDocumentProvider:
    def __init__(self, documents=None):
        self._documents = documents if documents is not None else []
        self.calls: list[tuple[str, date | None]] = []

    def get_documents(self, ticker, since=None):
        self.calls.append((ticker, since))
        return self._documents


def _raw_document(**overrides):
    record = {
        "document_date": date(2026, 2, 25),
        "document_type": "10-K",
        "title": "NVDA 10-K filed 2026-02-25",
        "text": "Revenue increased 20%.",
        "source": "https://www.sec.gov/example",
    }
    record.update(overrides)
    return record


def test_ingest_stores_new_documents(engine):
    provider = _FakeDocumentProvider([_raw_document()])
    stored = ingest_company_documents(engine, provider, "NVDA")
    assert stored == 1
    with Session(engine) as session:
        rows = session.scalars(select(CompanyDocument)).all()
    assert len(rows) == 1
    assert rows[0].ticker == "NVDA"
    assert rows[0].text == "Revenue increased 20%."
    assert rows[0].content_hash is not None
    assert rows[0].retrieved_at is not None


def test_ingest_is_idempotent_for_identical_documents(engine):
    provider = _FakeDocumentProvider([_raw_document()])
    ingest_company_documents(engine, provider, "NVDA")
    stored_second_run = ingest_company_documents(engine, provider, "NVDA")
    assert stored_second_run == 0
    with Session(engine) as session:
        assert len(session.scalars(select(CompanyDocument)).all()) == 1


def test_ingest_stores_multiple_distinct_documents(engine):
    provider = _FakeDocumentProvider([
        _raw_document(
            text="First filing text.", document_type="10-K",
            source="https://www.sec.gov/example/10k",
        ),
        _raw_document(
            text="Second filing text.", document_type="10-Q",
            source="https://www.sec.gov/example/10q",
        ),
    ])
    stored = ingest_company_documents(engine, provider, "NVDA")
    assert stored == 2


def test_ingest_with_no_documents_returns_zero_and_writes_nothing(engine):
    provider = _FakeDocumentProvider([])
    stored = ingest_company_documents(engine, provider, "NVDA")
    assert stored == 0
    with Session(engine) as session:
        assert session.scalars(select(CompanyDocument)).all() == []


def test_ingest_passes_since_through_to_the_provider(engine):
    provider = _FakeDocumentProvider([])
    since = date(2025, 1, 1)
    ingest_company_documents(engine, provider, "NVDA", since=since)
    assert provider.calls == [("NVDA", since)]


def test_ingest_normalizes_ticker_case(engine):
    provider = _FakeDocumentProvider([_raw_document()])
    ingest_company_documents(engine, provider, "nvda")
    with Session(engine) as session:
        row = session.scalars(select(CompanyDocument)).one()
    assert row.ticker == "NVDA"


def test_ingest_deduplicates_identical_text_within_the_same_batch(engine):
    """Regression test for a self-review finding: the dedup check was
    refactored from one SELECT per document to a single batched SELECT
    over all candidate hashes up front. That batching must not let two
    documents with identical text AND identical source in the SAME call
    both slip past the (now snapshot-in-time) existing_hashes set and get
    stored twice -- e.g. the provider genuinely returning the same filing
    twice in one response."""
    provider = _FakeDocumentProvider([
        _raw_document(text="Duplicated filing text.", document_type="10-K"),
        _raw_document(text="Duplicated filing text.", document_type="10-K"),
    ])
    stored = ingest_company_documents(engine, provider, "NVDA")
    assert stored == 1
    with Session(engine) as session:
        assert len(session.scalars(select(CompanyDocument)).all()) == 1


def test_ingest_stores_only_the_new_documents_in_a_mixed_batch(engine):
    """A second run against a provider that returns one already-stored
    document (same source) plus one genuinely new one (a distinct source)
    must store only the new one -- proving the batched existing-hash
    lookup still catches previously persisted rows, not just duplicates
    within a single call."""
    first_provider = _FakeDocumentProvider([
        _raw_document(
            text="Already stored.", document_type="10-K",
            source="https://www.sec.gov/example/10k",
        ),
    ])
    ingest_company_documents(engine, first_provider, "NVDA")

    second_provider = _FakeDocumentProvider([
        _raw_document(
            text="Already stored.", document_type="10-K",
            source="https://www.sec.gov/example/10k",
        ),
        _raw_document(
            text="Brand new filing.", document_type="10-Q",
            source="https://www.sec.gov/example/10q",
        ),
    ])
    stored = ingest_company_documents(engine, second_provider, "NVDA")
    assert stored == 1
    with Session(engine) as session:
        texts = set(session.scalars(select(CompanyDocument.text)).all())
    assert texts == {"Already stored.", "Brand new filing."}


def test_ingest_treats_identical_text_from_a_different_source_as_a_distinct_document(engine):
    """Regression test for a real correctness finding: two genuinely
    distinct SEC filings (e.g. a 10-K and a later 10-K/A amendment, each
    with its own accession number and its own `source` URL) can carry
    byte-identical extracted text -- a purely procedural amendment, or
    two exhibits sharing boilerplate -- but are still two separate real
    information events and must not collide into a single stored row
    just because their text happens to match. Dedup identity must be
    anchored on `source` (which already encodes SEC's own unique
    accession number/primary document), not on text alone."""
    provider = _FakeDocumentProvider([
        _raw_document(
            text="Same text, different filing.", document_type="10-K",
            source="https://www.sec.gov/example/10k-original",
        ),
        _raw_document(
            text="Same text, different filing.", document_type="10-K/A",
            source="https://www.sec.gov/example/10k-amendment",
        ),
    ])
    stored = ingest_company_documents(engine, provider, "NVDA")
    assert stored == 2
    with Session(engine) as session:
        types = set(session.scalars(select(CompanyDocument.document_type)).all())
    assert types == {"10-K", "10-K/A"}


def test_content_hash_falls_back_to_text_when_no_source_is_supplied(engine):
    """A provider that (unlike SECFilingDocumentProvider) supplies no
    source URL at all must still dedupe correctly, on text alone -- the
    fallback this codebase's own CompanyDocumentProvider interface
    doesn't strictly require every field to be present for."""
    provider = _FakeDocumentProvider([
        _raw_document(text="No source at all.", source=None),
        _raw_document(text="No source at all.", source=None),
    ])
    stored = ingest_company_documents(engine, provider, "NVDA")
    assert stored == 1
