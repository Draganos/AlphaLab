"""Conviction / Conflict Layer (roadmap Phase 8). See `assessment` for the
design rules and the gates this package must keep."""

from alpha_lab.conviction.assessment import (
    CONVICTION_METHODOLOGY_VERSION,
    FAMILY_LABELS,
    FAMILY_ORDER,
    FUNDAMENTAL_CATEGORIES,
    Agreement,
    Conflict,
    ConflictKind,
    ConflictStrength,
    ConvictionAssessment,
    Direction,
    EvidenceSide,
    Family,
    FamilyAssessment,
    FamilyLean,
    FamilyStatus,
    assess_conviction,
)
from alpha_lab.conviction.audit import (
    assessment_history,
    content_hash,
    input_fingerprint,
    record_assessment,
)

__all__ = [
    "CONVICTION_METHODOLOGY_VERSION",
    "FAMILY_LABELS",
    "FAMILY_ORDER",
    "FUNDAMENTAL_CATEGORIES",
    "Agreement",
    "Conflict",
    "ConflictKind",
    "ConflictStrength",
    "ConvictionAssessment",
    "Direction",
    "EvidenceSide",
    "Family",
    "FamilyAssessment",
    "FamilyLean",
    "FamilyStatus",
    "assess_conviction",
    "assessment_history",
    "content_hash",
    "input_fingerprint",
    "record_assessment",
]
