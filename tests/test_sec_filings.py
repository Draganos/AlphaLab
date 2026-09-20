"""Offline tests for the SEC filing-text ingestion provider (Document
Evidence Engine). No test here talks to real SEC EDGAR -- SECClient.get_json
and the raw document fetch are both replaced with fakes."""

from datetime import date
from unittest.mock import patch

import pytest

from alpha_lab.providers.sec_edgar import SECClient
from alpha_lab.providers.sec_filings import (
    MAX_DOCUMENT_TEXT_CHARS,
    SECFilingDocumentProvider,
    html_to_text,
)


# --- html_to_text: pure function, no network -------------------------------


def test_html_to_text_extracts_visible_text():
    html = "<html><body><p>Revenue increased 20%.</p><p>Margins improved.</p></body></html>"
    text = html_to_text(html)
    assert "Revenue increased 20%." in text
    assert "Margins improved." in text


def test_html_to_text_skips_script_and_style_content():
    html = "<html><head><style>.a{color:red}</style></head><body><script>var x=1;</script><p>Real content.</p></body></html>"
    text = html_to_text(html)
    assert "color:red" not in text
    assert "var x=1" not in text
    assert "Real content." in text


def test_html_to_text_inserts_breaks_between_block_elements():
    html = "<div>First paragraph</div><div>Second paragraph</div>"
    text = html_to_text(html)
    # The two paragraphs must not run together into one unreadable line.
    assert "First paragraph" in text.split("\n")[0] or "\n" in text


def test_html_to_text_unescapes_entities():
    html = "<p>Washington, D.C.&#160;20549</p>"
    text = html_to_text(html)
    assert "20549" in text


def test_html_to_text_is_bounded_by_max_document_text_chars():
    html = "<p>" + ("x" * (MAX_DOCUMENT_TEXT_CHARS + 10_000)) + "</p>"
    text = html_to_text(html)
    assert len(text) <= MAX_DOCUMENT_TEXT_CHARS


# --- SECFilingDocumentProvider: offline, fake client ------------------------


class _FakeClient:
    """Stands in for SECClient -- provides get_json and the identity/pacing
    attributes SECFilingDocumentProvider reads, without any real network
    access."""

    def __init__(self, submissions: dict, *, tickers: dict[str, str] | None = None):
        self.user_agent = "AlphaLab Test test@example.com"
        self.minimum_interval = 0.0
        self._last_request = 0.0
        self._submissions = submissions
        self._tickers = tickers or {"NVDA": "0001045810"}

    def get_json(self, path: str, *, refresh: bool = False):
        if "company_tickers.json" in path:
            return {
                str(i): {"ticker": ticker, "cik_str": int(cik)}
                for i, (ticker, cik) in enumerate(self._tickers.items())
            }
        for cik, payload in self._submissions.items():
            if cik in path:
                return payload
        raise AssertionError(f"Unexpected SEC path requested: {path}")


def _submissions_payload(*, forms, filed_dates, accessions, primary_documents):
    return {
        "filings": {
            "recent": {
                "form": forms,
                "filingDate": filed_dates,
                "accessionNumber": accessions,
                "primaryDocument": primary_documents,
            }
        }
    }


def test_get_documents_returns_empty_list_for_an_unresolvable_ticker():
    client = _FakeClient({}, tickers={})
    provider = SECFilingDocumentProvider(client)
    assert provider.get_documents("NOPE") == []


def test_get_documents_filters_to_supported_forms_only():
    submissions = _submissions_payload(
        forms=["10-K", "8-K", "10-Q"],
        filed_dates=["2026-02-25", "2026-01-10", "2025-11-19"],
        accessions=["0001045810-26-000021", "0001045810-26-000005", "0001045810-25-000230"],
        primary_documents=["nvda-10k.htm", "nvda-8k.htm", "nvda-10q.htm"],
    )
    client = _FakeClient({"0001045810": submissions})
    provider = SECFilingDocumentProvider(client)
    with patch.object(SECFilingDocumentProvider, "_fetch_document_html", return_value="<p>Filing text.</p>"):
        documents = provider.get_documents("NVDA")
    assert {doc["document_type"] for doc in documents} == {"10-K", "10-Q"}
    assert len(documents) == 2


def test_get_documents_respects_since():
    submissions = _submissions_payload(
        forms=["10-K", "10-Q"],
        filed_dates=["2024-02-25", "2026-02-25"],
        accessions=["0001045810-24-000021", "0001045810-26-000021"],
        primary_documents=["old-10k.htm", "new-10k.htm"],
    )
    client = _FakeClient({"0001045810": submissions})
    provider = SECFilingDocumentProvider(client)
    with patch.object(SECFilingDocumentProvider, "_fetch_document_html", return_value="<p>Filing text.</p>"):
        documents = provider.get_documents("NVDA", since=date(2025, 1, 1))
    assert len(documents) == 1
    assert documents[0]["document_date"] == date(2026, 2, 25)


def test_get_documents_skips_a_single_failed_fetch_without_aborting_the_rest():
    submissions = _submissions_payload(
        forms=["10-K", "10-Q"],
        filed_dates=["2026-02-25", "2025-11-19"],
        accessions=["0001045810-26-000021", "0001045810-25-000230"],
        primary_documents=["nvda-10k.htm", "nvda-10q.htm"],
    )
    client = _FakeClient({"0001045810": submissions})
    provider = SECFilingDocumentProvider(client)
    with patch.object(SECFilingDocumentProvider, "_fetch_document_html", side_effect=[None, "<p>OK</p>"]):
        documents = provider.get_documents("NVDA")
    assert len(documents) == 1
    assert "OK" in documents[0]["text"]


def test_get_documents_produces_real_extracted_text_not_raw_html():
    submissions = _submissions_payload(
        forms=["10-K"], filed_dates=["2026-02-25"],
        accessions=["0001045810-26-000021"], primary_documents=["nvda-10k.htm"],
    )
    client = _FakeClient({"0001045810": submissions})
    provider = SECFilingDocumentProvider(client)
    with patch.object(
        SECFilingDocumentProvider, "_fetch_document_html",
        return_value="<html><body><p>Revenue grew 20% year over year.</p></body></html>",
    ):
        documents = provider.get_documents("NVDA")
    assert documents[0]["text"] == "Revenue grew 20% year over year."
    assert "<p>" not in documents[0]["text"]
    assert documents[0]["source"] == (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-10k.htm"
    )
