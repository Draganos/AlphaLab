"""SEC EDGAR 10-K/10-Q filing text ingestion -- the `CompanyDocumentProvider`
implementation the project's very first PR declared as an interface but
never built (see ARCHITECTURE.md's Evidence Coverage Hardening
investigation). Reuses `SECClient`'s existing identity/pacing/caching
rather than a second HTTP client, and `SECCompanyFactsProvider.
company_tickers()`'s existing ticker->CIK resolution rather than a new
lookup.

Real filing text only: this never fabricates a document, never guesses at
content, and skips a ticker entirely (returning `[]`, not a partial or
placeholder document) when SEC EDGAR has no CIK or no supported filing for
it -- exactly `NewsService.refresh`'s own "missing evidence is reported as
missing, never invented" pattern applied to filings instead of articles.
"""

from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import re
import time

from alpha_lab.providers.interfaces import CompanyDocumentProvider
from alpha_lab.providers.sec_edgar import SUPPORTED_FORMS, SECClient, SECCompanyFactsProvider

# A defensive upper bound on extracted plain text per filing -- a modern
# 10-K's raw inline-XBRL HTML can run several megabytes; stripped to plain
# text it is typically a few hundred KB. This is not a content judgement
# (nothing meaningful is ever truncated mid-thought on purpose) -- it is
# purely a guard against a pathological filing consuming unbounded storage,
# mirroring the general principle of bounding untrusted external input.
MAX_DOCUMENT_TEXT_CHARS = 500_000

# Tags whose entire contents are never real prose -- inline XBRL metadata,
# scripts, and styling. Skipping their content (not just the tags) avoids
# polluting extracted text with raw XBRL tag names or CSS/JS source.
_SKIP_CONTENT_TAGS = {"script", "style", "ix:header"}

# Tags that represent a natural break between chunks of prose; a newline is
# inserted at each one so sentences from adjacent table cells/paragraphs
# don't run together into one unreadable line.
_BLOCK_BREAK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}


class _FilingTextExtractor(HTMLParser):
    """Minimal, dependency-free HTML-to-text extraction (stdlib only, per
    this project's established preference for not adding a dependency --
    see the Signal Predictive-Value phase's own scipy-avoidance -- when
    the standard library already does the job). Not a general-purpose HTML
    renderer: it only needs to turn a filing into readable plain text for
    keyword/phrase-based analysis, not preserve layout or tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_BREAK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_BREAK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        # Collapse runs of whitespace within a line, and collapse more than
        # two consecutive newlines down to two (paragraph break) -- the raw
        # extraction otherwise leaves long runs of blank lines from empty
        # table cells and nested layout divs.
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def html_to_text(html: str) -> str:
    """Real extraction, never a summary or a guess: every word returned was
    literally present in `html`. Bounded by `MAX_DOCUMENT_TEXT_CHARS`."""
    parser = _FilingTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()[:MAX_DOCUMENT_TEXT_CHARS]


class SECFilingDocumentProvider(CompanyDocumentProvider):
    """`alpha_lab.providers.interfaces.CompanyDocumentProvider` implementation
    -- the concrete piece that interface was always missing. Only 10-K/10-Q
    (and their amendments), matching `SECCompanyFactsProvider`'s own
    `SUPPORTED_FORMS` scope; 8-K, press releases, and any non-SEC source are
    deliberately out of scope for this first version (see ARCHITECTURE.md).
    """

    provider_name = "SECFilingDocumentProvider"

    def __init__(self, client: SECClient):
        self.client = client
        self._facts_provider = SECCompanyFactsProvider(client)

    def get_documents(self, ticker: str, since: date | None = None) -> list[dict[str, Any]]:
        normalized = ticker.strip().upper()
        cik = self._facts_provider.company_tickers().get(normalized)
        if cik is None:
            return []
        submissions = self.client.get_json(f"/submissions/CIK{cik}.json")
        recent = submissions.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        filed_dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_documents = recent.get("primaryDocument", [])

        documents: list[dict[str, Any]] = []
        for form, filed_date_raw, accession, primary_document in zip(
            forms, filed_dates, accessions, primary_documents
        ):
            if form not in SUPPORTED_FORMS:
                continue
            filed_date = date.fromisoformat(filed_date_raw)
            if since is not None and filed_date < since:
                continue
            accession_nodash = accession.replace("-", "")
            cik_nodash = cik.lstrip("0") or "0"
            source_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_nodash}/{accession_nodash}/{primary_document}"
            )
            html = self._fetch_document_html(source_url)
            if html is None:
                continue
            documents.append({
                "document_date": filed_date,
                "document_type": form,
                "title": f"{normalized} {form} filed {filed_date.isoformat()}",
                "text": html_to_text(html),
                "source": source_url,
            })
        return documents

    def _fetch_document_html(self, url: str) -> str | None:
        """A single filing document fetch failing (network, a 404 for a
        primary document SEC's own index listed but no longer serves) skips
        just that one document -- mirrors `NewsService.refresh`'s "one item
        failing never aborts the batch" rule -- rather than aborting every
        other filing for this ticker."""
        wait = self.client.minimum_interval - (time.monotonic() - self.client._last_request)
        if wait > 0:
            time.sleep(wait)
        request = Request(url, headers={"User-Agent": self.client.user_agent})
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed SEC EDGAR host
                html = response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError):
            return None
        self.client._last_request = time.monotonic()
        return html
