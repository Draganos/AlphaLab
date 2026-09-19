"""AlphaLab Research Stance (PR #31): cross-domain synthesis of already-
computed research evidence into one inspectable, traceable summary. See
`alpha_lab.research_stance.stance` for the full scope rationale and
`build_research_stance` for the public entry point. Nothing here computes
a new 0-100 score, and nothing here is imported by any scoring/ranking
module.
"""

from alpha_lab.research_stance.stance import (
    DOMAIN_LABELS,
    DOMAIN_ORDER,
    RESEARCH_STANCE_METHODOLOGY_VERSION,
    ResearchStance,
    ResearchStanceOutcome,
    StanceLean,
    StanceLine,
    build_research_stance,
)

__all__ = [
    "DOMAIN_LABELS",
    "DOMAIN_ORDER",
    "RESEARCH_STANCE_METHODOLOGY_VERSION",
    "ResearchStance",
    "ResearchStanceOutcome",
    "StanceLean",
    "StanceLine",
    "build_research_stance",
]
