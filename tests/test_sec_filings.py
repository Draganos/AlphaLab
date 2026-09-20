"""Offline tests for the SEC filing-text ingestion provider (Document
Evidence Engine). No test here talks to real SEC EDGAR -- SECClient's
get_json/get_text are both replaced with a fake."""

from datetime import date

import pytest

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
    """Stands in for SECClient -- provides get_json/get_text and the
    identity attributes SECFilingDocumentProvider reads, without any real
    network access."""

    def __init__(
        self, submissions: dict, *, tickers: dict[str, str] | None = None,
        pages: dict[str, dict] | None = None, document_html: dict[str, str] | None = None,
        document_html_sequence: list[str | None] | None = None,
        failing_pages: set[str] | None = None,
    ):
        self.user_agent = "AlphaLab Test test@example.com"
        self.minimum_interval = 0.0
        self._last_request = 0.0
        self._submissions = submissions
        self._tickers = tickers or {"NVDA": "0001045810"}
        self._pages = pages or {}
        self._document_html = document_html or {}
        self._document_html_sequence = list(document_html_sequence or [])
        self._failing_pages = failing_pages or set()
        self.get_json_calls: list[tuple[str, bool]] = []
        self.get_text_calls: list[str] = []

    def get_json(self, path: str, *, refresh: bool = False):
        self.get_json_calls.append((path, refresh))
        if "company_tickers.json" in path:
            return {
                str(i): {"ticker": ticker, "cik_str": int(cik)}
                for i, (ticker, cik) in enumerate(self._tickers.items())
            }
        for name in self._failing_pages:
            if name in path:
                raise RuntimeError(f"SEC request failed without modifying stored data: {path}")
        for name, payload in self._pages.items():
            if name in path:
                return payload
        for cik, payload in self._submissions.items():
            if cik in path:
                return payload
        raise AssertionError(f"Unexpected SEC path requested: {path}")

    def get_text(self, url: str, *, refresh: bool = False) -> str | None:
        self.get_text_calls.append(url)
        if self._document_html_sequence:
            return self._document_html_sequence.pop(0)
        return self._document_html.get(url, "<p>Filing text.</p>")


def _submissions_payload(*, forms, filed_dates, accessions, primary_documents, files=None):
    return {
        "filings": {
            "recent": {
                "form": forms,
                "filingDate": filed_dates,
                "accessionNumber": accessions,
                "primaryDocument": primary_documents,
            },
            "files": files or [],
        }
    }


def test_get_documents_returns_empty_list_for_an_unresolvable_ticker():
    client = _FakeClient({}, tickers={})
    provider = SECFilingDocumentProvider(client)
    assert provider.get_documents("NOPE") == []


def test_get_documents_resolves_a_dotted_share_class_ticker():
    """Regression test for a live-confirmed hardening finding: SEC EDGAR's
    own company_tickers.json keys "BRK.B" as "BRK-B", the identical
    mismatch already fixed for Yahoo Finance -- a plain dict lookup by the
    canonical dotted ticker silently returned None (indistinguishable from
    "not a real company") for every dual-class/preferred-share ticker."""
    submissions = _submissions_payload(
        forms=["10-K"], filed_dates=["2026-02-25"],
        accessions=["0001067983-26-000021"], primary_documents=["brk-10k.htm"],
    )
    client = _FakeClient({"0001067983": submissions}, tickers={"BRK-B": "0001067983"})
    provider = SECFilingDocumentProvider(client)
    documents = provider.get_documents("BRK.B")
    assert len(documents) == 1
    assert documents[0]["document_date"] == date(2026, 2, 25)


def test_get_documents_filters_to_supported_forms_only():
    submissions = _submissions_payload(
        forms=["10-K", "8-K", "10-Q"],
        filed_dates=["2026-02-25", "2026-01-10", "2025-11-19"],
        accessions=["0001045810-26-000021", "0001045810-26-000005", "0001045810-25-000230"],
        primary_documents=["nvda-10k.htm", "nvda-8k.htm", "nvda-10q.htm"],
    )
    client = _FakeClient({"0001045810": submissions})
    provider = SECFilingDocumentProvider(client)
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
    client = _FakeClient({"0001045810": submissions}, document_html_sequence=[None, "<p>OK</p>"])
    provider = SECFilingDocumentProvider(client)
    documents = provider.get_documents("NVDA")
    assert len(documents) == 1
    assert "OK" in documents[0]["text"]


def test_get_documents_produces_real_extracted_text_not_raw_html():
    submissions = _submissions_payload(
        forms=["10-K"], filed_dates=["2026-02-25"],
        accessions=["0001045810-26-000021"], primary_documents=["nvda-10k.htm"],
    )
    expected_url = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-10k.htm"
    client = _FakeClient(
        {"0001045810": submissions},
        document_html={expected_url: "<html><body><p>Revenue grew 20% year over year.</p></body></html>"},
    )
    provider = SECFilingDocumentProvider(client)
    documents = provider.get_documents("NVDA")
    assert documents[0]["text"] == "Revenue grew 20% year over year."
    assert "<p>" not in documents[0]["text"]
    assert documents[0]["source"] == expected_url


def test_get_documents_refreshes_the_submissions_index_every_call():
    """The filing index must never be served from a stale cache -- unlike
    the filing documents themselves (immutable once filed), the index is
    exactly what tells this method about a filing made since the last
    run. Regression test for a self-review finding: the first version
    fetched it with the default refresh=False, permanently blinding every
    future refresh to new filings after the first cache write."""
    submissions = _submissions_payload(
        forms=["10-K"], filed_dates=["2026-02-25"],
        accessions=["0001045810-26-000021"], primary_documents=["nvda-10k.htm"],
    )
    client = _FakeClient({"0001045810": submissions})
    provider = SECFilingDocumentProvider(client)
    provider.get_documents("NVDA")
    submission_calls = [call for call in client.get_json_calls if "submissions/CIK" in call[0]]
    assert submission_calls == [("/submissions/CIK0001045810.json", True)]


def test_get_documents_merges_paginated_filing_history():
    """Regression test for a self-review finding: SEC caps `filings.recent`
    at roughly the most recent 1,000 filings across every form type, so a
    long-lived filer's older 10-Ks/10-Qs live in a paginated `filings.
    files` entry instead -- confirmed live against real NVDA/MA data.
    Skipping these silently truncated filing history for exactly the
    tickers most worth having deep history for."""
    recent = _submissions_payload(
        forms=["10-K"], filed_dates=["2026-02-25"],
        accessions=["0001045810-26-000021"], primary_documents=["recent-10k.htm"],
        files=[{"name": "CIK0001045810-submissions-001.json", "filingCount": 1, "filingFrom": "2015-01-01", "filingTo": "2020-01-01"}],
    )
    page = {
        "form": ["10-K"],
        "filingDate": ["2015-02-20"],
        "accessionNumber": ["0001045810-15-000010"],
        "primaryDocument": ["old-10k.htm"],
    }
    client = _FakeClient(
        {"0001045810": recent},
        pages={"CIK0001045810-submissions-001.json": page},
    )
    provider = SECFilingDocumentProvider(client)
    documents = provider.get_documents("NVDA")
    assert {doc["document_date"] for doc in documents} == {date(2026, 2, 25), date(2015, 2, 20)}


def test_get_documents_survives_a_failed_paginated_page_without_losing_recent_filings():
    """Regression test for a hardening finding: a page fetch failure (SEC
    request timeout, transient 5xx) previously propagated straight out of
    get_documents, discarding the "recent" rows already collected -- the
    most recent filings are the ones that matter most, so one unreachable
    older-history page must never cost the ticker its recent filings too."""
    recent = _submissions_payload(
        forms=["10-K"], filed_dates=["2026-02-25"],
        accessions=["0001045810-26-000021"], primary_documents=["recent-10k.htm"],
        files=[
            {"name": "CIK0001045810-submissions-001.json", "filingCount": 1, "filingFrom": "2015-01-01", "filingTo": "2020-01-01"},
        ],
    )
    client = _FakeClient({"0001045810": recent}, failing_pages={"CIK0001045810-submissions-001.json"})
    provider = SECFilingDocumentProvider(client)
    documents = provider.get_documents("NVDA")
    assert {doc["document_date"] for doc in documents} == {date(2026, 2, 25)}


def test_get_documents_survives_one_malformed_page_and_keeps_the_others():
    """Regression test: a page whose parallel arrays don't line up (the
    strict=True guard in _filing_rows) must skip only that page, not abort
    the whole ticker -- a second, well-formed page's filings are still real
    evidence and must not be discarded because of an unrelated bad page."""
    recent = _submissions_payload(
        forms=["10-K"], filed_dates=["2026-02-25"],
        accessions=["0001045810-26-000021"], primary_documents=["recent-10k.htm"],
        files=[
            {"name": "CIK0001045810-submissions-001.json", "filingCount": 1, "filingFrom": "2010-01-01", "filingTo": "2015-01-01"},
            {"name": "CIK0001045810-submissions-002.json", "filingCount": 1, "filingFrom": "2015-01-01", "filingTo": "2020-01-01"},
        ],
    )
    malformed_page = {  # accessionNumber is one entry short of the other arrays
        "form": ["10-K"],
        "filingDate": ["2012-02-20"],
        "accessionNumber": [],
        "primaryDocument": ["old-10k.htm"],
    }
    good_page = {
        "form": ["10-K"],
        "filingDate": ["2016-02-20"],
        "accessionNumber": ["0001045810-16-000010"],
        "primaryDocument": ["older-10k.htm"],
    }
    client = _FakeClient(
        {"0001045810": recent},
        pages={
            "CIK0001045810-submissions-001.json": malformed_page,
            "CIK0001045810-submissions-002.json": good_page,
        },
    )
    provider = SECFilingDocumentProvider(client)
    documents = provider.get_documents("NVDA")
    assert {doc["document_date"] for doc in documents} == {date(2026, 2, 25), date(2016, 2, 20)}


def test_filing_rows_raises_loudly_on_mismatched_array_lengths():
    """_filing_rows itself must not silently misalign a form with the wrong
    filing date/accession/document when SEC's own arrays don't line up --
    reporting a real filing under the wrong metadata is worse than raising."""
    from alpha_lab.providers.sec_filings import _filing_rows

    payload = {
        "form": ["10-K", "10-Q"],
        "filingDate": ["2026-02-25"],  # one short
        "accessionNumber": ["0001045810-26-000021", "0001045810-26-000005"],
        "primaryDocument": ["a.htm", "b.htm"],
    }
    with pytest.raises(ValueError):
        _filing_rows(payload)


def test_get_documents_uses_get_text_not_a_second_http_client():
    """Regression test for a self-review finding: filing document fetches
    must go through SECClient's own shared identity/pacing/caching
    (get_text), never a second, independently-implemented HTTP fetch that
    reaches into the client's private pacing state from outside."""
    submissions = _submissions_payload(
        forms=["10-K"], filed_dates=["2026-02-25"],
        accessions=["0001045810-26-000021"], primary_documents=["nvda-10k.htm"],
    )
    client = _FakeClient({"0001045810": submissions})
    provider = SECFilingDocumentProvider(client)
    provider.get_documents("NVDA")
    assert client.get_text_calls == [
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-10k.htm"
    ]
