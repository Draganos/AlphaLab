"""Shared ticker-notation translation for external providers whose own
symbol format differs from AlphaLab's canonical share-class/preferred
notation (`Security.ticker`, e.g. "BRK.B", "AHL$D").

Originally discovered and fixed for Yahoo Finance only (see
`yfinance_provider._yahoo_symbol`'s own history: two real notations
confirmed live to 404 permanently against Yahoo until translated). While
hardening SEC filing ingestion, the exact same mismatch was confirmed live
against SEC EDGAR's own `company_tickers.json` -- it too keys tickers with
a hyphen ("BRK-B"), not AlphaLab's canonical dot/dollar-sign notation, so
`SECCompanyFactsProvider.company_tickers()` silently failed to resolve a
CIK for any dual-class or preferred-share ticker exactly the way Yahoo did.
Extracted here once both providers needed the identical translation,
rather than reimplementing it a second time.
"""

import re

# A genuine AlphaLab share-class suffix is one or two letters (A, B, K,
# WI, ...). A blanket ticker.replace(".", "-") was confirmed live to
# regress a real macro-proxy ticker: "DX-Y.NYB" (the US Dollar Index) is
# ALREADY Yahoo's own correct symbol -- its dot is not a share-class
# separator at all -- and blindly hyphenating it produces "DX-Y-NYB",
# which 404s. Only a dot followed by a short all-letter suffix is treated
# as a share class; anything else (a longer or non-letter suffix) is left
# untouched.
_SHARE_CLASS_SUFFIX = re.compile(r"^[A-Z]{1,2}$")


def to_hyphenated_symbol(ticker: str) -> str:
    """A dot-separated share class (e.g. "BRK.B", "AGM.A") resolves as a
    hyphen ("BRK-B", "AGM-A"), and a dollar-sign preferred-share suffix
    (e.g. "AHL$D", "EPR$E") resolves as a hyphen plus "P" ("AHL-PD",
    "EPR-PE") -- both verified live against Yahoo Finance and SEC EDGAR.
    A ticker whose dot is already part of the provider's own real symbol
    (e.g. "DX-Y.NYB") is left unchanged -- see `_SHARE_CLASS_SUFFIX`."""
    if "$" in ticker:
        base, _, suffix = ticker.partition("$")
        return f"{base}-P{suffix}"
    if "." in ticker:
        base, _, suffix = ticker.rpartition(".")
        if _SHARE_CLASS_SUFFIX.match(suffix):
            return f"{base}-{suffix}"
    return ticker
