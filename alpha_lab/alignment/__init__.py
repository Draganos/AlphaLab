"""AlphaLab Phase 2B: Donatien <-> Market Regime Alignment.

A small, categorical-only comparison layer between two existing,
independent evidence layers (AlphaLab Macro Regime and Donatien External
Calibration). See `alpha_lab.alignment.alignment` for the deterministic
mapping and `alpha_lab.alignment.service` for persistence. Nothing here
computes a score, and nothing here is imported by any scoring/ranking
module -- see the module docstrings for the full scope rationale.
"""

from alpha_lab.alignment.alignment import (
    ALIGNMENT_METHODOLOGY_VERSION,
    Alignment,
    AlignmentAssessment,
    DonatienLean,
    build_alignment_assessment,
    classify_donatien_lean,
)
from alpha_lab.alignment.service import AlignmentService

__all__ = [
    "ALIGNMENT_METHODOLOGY_VERSION",
    "Alignment",
    "AlignmentAssessment",
    "DonatienLean",
    "build_alignment_assessment",
    "classify_donatien_lean",
    "AlignmentService",
]
