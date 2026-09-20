"""Tests for alpha_lab.ai.documents.select_documents_for_analysis and its
use in AIResearchService.ensure_all -- the fix for a real finding from the
Signal Predictive-Value calibration study: RuleBasedFinancialResearchProvider's
ai_rating trended upward over time as more filings accumulated, because
scores were a running phrase-count over the ENTIRE cumulative document set
rather than a bounded, recent window reflecting current state."""

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from alpha_lab.ai.documents import DOCUMENT_ANALYSIS_WINDOW_DAYS, select_documents_for_analysis
from alpha_lab.ai.research import AIResearchProvider, AIResearchResult
from alpha_lab.ai.service import AIResearchService
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import CompanyDocument, Security


def _document(document_id: int, document_date: date, text: str = "text") -> CompanyDocument:
    return CompanyDocument(
        ticker="X", document_date=document_date, document_type="10-K",
        title=f"Document {document_id}", text=text,
    )


# --- select_documents_for_analysis: pure function, no DB -------------------


def test_select_documents_for_analysis_excludes_filings_older_than_the_window():
    as_of = date(2026, 1, 1)
    recent = _document(1, as_of - timedelta(days=10))
    old = _document(2, as_of - timedelta(days=DOCUMENT_ANALYSIS_WINDOW_DAYS + 1))
    selected = select_documents_for_analysis([recent, old], as_of=as_of)
    assert selected == [recent]


def test_select_documents_for_analysis_boundary_is_exclusive_on_the_far_edge():
    as_of = date(2026, 1, 1)
    just_inside = _document(1, as_of - timedelta(days=DOCUMENT_ANALYSIS_WINDOW_DAYS - 1))
    exactly_at_cutoff = _document(2, as_of - timedelta(days=DOCUMENT_ANALYSIS_WINDOW_DAYS))
    selected = select_documents_for_analysis([just_inside, exactly_at_cutoff], as_of=as_of)
    assert selected == [just_inside]


def test_select_documents_for_analysis_excludes_filings_after_as_of():
    """PIT safety: a document dated after as_of must never be included,
    even though this can't happen for AIResearchService.ensure_all's own
    as_of=today call, it matters for the calibration study's historical
    replay."""
    as_of = date(2026, 1, 1)
    future = _document(1, as_of + timedelta(days=1))
    selected = select_documents_for_analysis([future], as_of=as_of)
    assert selected == []


def test_select_documents_for_analysis_returns_empty_for_no_documents():
    assert select_documents_for_analysis([], as_of=date(2026, 1, 1)) == []


# --- AIResearchService.ensure_all: windowing applied in production ---------


class _CountingProvider(AIResearchProvider):
    def __init__(self):
        self.seen_document_ids: list[list[int]] = []

    def analyze(self, ticker, documents):
        self.seen_document_ids.append([document["id"] for document in documents])
        return AIResearchResult(
            guidance_score=0, demand_score=0, margin_outlook_score=0,
            competitive_position_score=0, management_confidence_score=0,
            balance_sheet_commentary_score=0, risk_score=0, sentiment_score=0,
            catalyst_score=0, evidence=[], summary="s", provider="counting",
            model="fixture", prompt_version="v1", confidence=1,
        )


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="X"))
        session.commit()
    try:
        yield engine
    finally:
        engine.dispose()


def test_ensure_all_only_analyzes_the_windowed_documents(engine):
    """Regression test for the real score-saturation finding: a filing
    from years ago must not be fed into the analysis just because it
    still exists in CompanyDocument -- only the trailing window."""
    with Session(engine) as session:
        session.add(CompanyDocument(
            ticker="X", document_date=date.today() - timedelta(days=10),
            document_type="10-K", title="Recent", text="Recent text.",
        ))
        session.add(CompanyDocument(
            ticker="X", document_date=date.today() - timedelta(days=DOCUMENT_ANALYSIS_WINDOW_DAYS + 30),
            document_type="10-K", title="Old", text="Old text.",
        ))
        session.commit()

    provider = _CountingProvider()
    service = AIResearchService(engine, provider)
    stored = service.ensure_all()

    assert stored == 1
    assert len(provider.seen_document_ids) == 1
    with Session(engine) as session:
        titles = {
            document.title
            for document in session.query(CompanyDocument).all()
        }
    assert titles == {"Recent", "Old"}  # the old document is untouched, not deleted


def test_ensure_all_stores_nothing_when_every_document_has_aged_out_of_the_window(engine):
    with Session(engine) as session:
        session.add(CompanyDocument(
            ticker="X", document_date=date.today() - timedelta(days=DOCUMENT_ANALYSIS_WINDOW_DAYS + 30),
            document_type="10-K", title="Old", text="Old text.",
        ))
        session.commit()

    provider = _CountingProvider()
    service = AIResearchService(engine, provider)
    assert service.ensure_all() == 0
    assert provider.seen_document_ids == []
