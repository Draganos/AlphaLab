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
        _raw_document(text="First filing text.", document_type="10-K"),
        _raw_document(text="Second filing text.", document_type="10-Q"),
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
