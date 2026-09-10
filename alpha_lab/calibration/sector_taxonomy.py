"""Morningstar (yfinance) sector -> approximate GICS sector name
correspondence.

Scope note: AlphaLab's `Security.sector` is sourced from yfinance, which
follows Morningstar's 11-sector classification, NOT true GICS -- despite
both systems having 11 broadly similar sectors, their names and the
company-level classification boundaries differ (this is a well-documented
data-quality gap, not an AlphaLab invention). Donatien's
`gics_sector` field, by contrast, explicitly claims real GICS
classification.

`MORNINGSTAR_TO_GICS_SECTOR` below is a **name correspondence only** --
Morningstar's well-known sector-naming parallel to GICS (both classification
schemes were designed around the same 11 broad sector concepts, just with
different names for several of them). It is NOT a verified, company-level
equivalence: a specific company's Morningstar sector and its true GICS
sector can still diverge at the boundaries even when the sector *names*
correspond here. This table must never be presented as authoritative GICS
data -- only as a disclosed, best-effort bridge, exactly the same
transparency standard already applied to `alpha_lab.macro.regime`'s
VIX/yield-curve bands and the Alignment package's scenario-weight
quadrant classification.
"""

MORNINGSTAR_GICS_BRIDGE_VERSION = "morningstar-gics-bridge-v1"

# Morningstar sector name (as returned by yfinance's `sector` field) ->
# best-effort corresponding GICS sector name.
MORNINGSTAR_TO_GICS_SECTOR: dict[str, str] = {
    "Technology": "Information Technology",
    "Financial Services": "Financials",
    "Healthcare": "Health Care",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Basic Materials": "Materials",
    "Communication Services": "Communication Services",
}


def approximate_gics_sector(morningstar_sector: str | None) -> str | None:
    """Best-effort GICS sector name for a Morningstar sector string.

    Returns None -- never a guess -- if `morningstar_sector` is None or is
    not one of the 11 recognized Morningstar sector names (e.g. a stale,
    malformed, or non-US-scheme value from the provider)."""
    if morningstar_sector is None:
        return None
    return MORNINGSTAR_TO_GICS_SECTOR.get(morningstar_sector)
