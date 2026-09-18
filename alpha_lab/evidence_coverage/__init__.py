"""AlphaLab Research Evidence & Coverage Dashboard (PR #28).

A read-only layer that combines the fundamental/Analyst Consensus/Analyst
Research/Technical Summary/AI Research Rating evidence already on
`StockResearch` with the separately-owned News Engine and Macro Regime
evidence into one per-security coverage view. See
`alpha_lab.evidence_coverage.summary` for the full scope rationale and
`build_security_coverage_summary`/`flatten_coverage_rows` for the two
public entry points. Nothing here computes a score, and nothing here is
imported by any scoring/ranking module.
"""

from alpha_lab.evidence_coverage.summary import (
    COVERAGE_SUMMARY_METHODOLOGY_VERSION,
    CoverageRow,
    CoverageStatus,
    SecurityCoverageSummary,
    build_security_coverage_summary,
    flatten_coverage_rows,
    summarize_universe_breakdown,
)

__all__ = [
    "COVERAGE_SUMMARY_METHODOLOGY_VERSION",
    "CoverageRow",
    "CoverageStatus",
    "SecurityCoverageSummary",
    "build_security_coverage_summary",
    "flatten_coverage_rows",
    "summarize_universe_breakdown",
]
