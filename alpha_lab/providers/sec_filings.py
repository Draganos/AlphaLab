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
import re

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
        cik = self._facts_provider.resolve_cik(normalized)
        if cik is None:
            return []
        # refresh=True: unlike XBRL facts (used for backtesting, where a
        # cached snapshot is fine), the filing INDEX is what tells this
        # method about a filing made since the last run -- caching it
        # forever would permanently blind every future refresh to new
        # 10-Ks/10-Qs. The filing documents themselves (fetched below via
        # get_text, default refresh=False) are immutable once filed, so
        # caching those forever is correct and desirable.
        submissions = self.client.get_json(f"/submissions/CIK{cik}.json", refresh=True)
        rows = list(_filing_rows(submissions.get("filings", {}).get("recent", {})))
        # SEC caps "recent" at roughly the most recent 1,000 filings across
        # every form type (8-K, Form 4, proxies, ... not just 10-K/10-Q),
        # so a long-lived or actively-filing issuer's older 10-Ks/10-Qs
        # live in one or more paginated `filings.files` entries instead --
        # confirmed live: NVDA and MA (both filing since the late 1990s/
        # early 2000s) each have exactly one such page. Skipping these
        # would silently truncate filing history for exactly the tickers
        # most worth having deep history for.
        #
        # A single page failing (network, a malformed/truncated payload)
        # must not discard the "recent" rows already collected above, nor
        # abort the other pages -- the most recent filings are the ones
        # that matter most, and one bad older-history page is never a
        # reason to lose them. Mirrors this method's own "one filing
        # document failing skips just that one" rule below, applied to a
        # page of the filing index instead of a single document.
        for page in submissions.get("filings", {}).get("files", []):
            name = page.get("name")
            if not name:
                continue
            try:
                page_payload = self.client.get_json(f"/submissions/{name}")
                rows.extend(_filing_rows(page_payload))
            except (RuntimeError, ValueError):
                continue

        documents: list[dict[str, Any]] = []
        for form, filed_date, accession, primary_document in rows:
            if form not in SUPPORTED_FORMS:
                continue
            if since is not None and filed_date < since:
                continue
            accession_nodash = accession.replace("-", "")
            cik_nodash = cik.lstrip("0") or "0"
            source_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_nodash}/{accession_nodash}/{primary_document}"
            )
            # A single filing document failing (network, a 404 for a
            # primary document SEC's own index listed but no longer
            # serves) skips just that one document -- mirrors
            # NewsService.refresh's "one item failing never aborts the
            # batch" rule -- rather than aborting every other filing for
            # this ticker. get_text also caches each real filing to disk
            # (a filing's own text is immutable once filed), so a later
            # re-run never re-downloads one already fetched.
            html = self.client.get_text(source_url)
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


def _filing_rows(payload: dict[str, Any]) -> list[tuple[str, date, str, str]]:
    """(form, filed_date, accession, primary_document) rows from either the
    top-level `filings.recent` object or a paginated `filings.files` page --
    both share the same parallel-array shape, just at different nesting.

    `strict=True`: these four arrays must be the same length, since each
    index is one filing's (form, date, accession, document) tuple. Silently
    zipping mismatched-length arrays (the default) would misalign a form
    with the wrong filing date or accession rather than fail loudly --
    reporting a real filing under the wrong metadata is worse than raising,
    since nothing downstream could tell the two apart."""
    forms = payload.get("form", [])
    filed_dates = payload.get("filingDate", [])
    accessions = payload.get("accessionNumber", [])
    primary_documents = payload.get("primaryDocument", [])
    return [
        (form, date.fromisoformat(filed_date_raw), accession, primary_document)
        for form, filed_date_raw, accession, primary_document in zip(
            forms, filed_dates, accessions, primary_documents, strict=True
        )
    ]
