"""roadmap Phase 6: bridges `alpha_lab.research_state.ResearchState` (Phase
5's canonical, per-security assembler) into `alpha_lab.research.ai_rating`'s
existing evidence-bounded AI Research Rating.

Deliberately a separate module, not a change to either `research_state.py`
(which stays a pure assembler -- see its own docstring) or `ai_rating.py`'s
existing `build_evidence_payload` (left untouched so every existing caller
and test keeps its exact current evidence set and behavior). This module
only adds: it calls the existing `build_evidence_payload` for the
StockResearch-derived evidence exactly as before, then extends that list
with evidence AlphaLab already assembles into `ResearchState` but that
`build_evidence_payload` has never had visibility into -- Macro, Donatien,
Donatien/Macro Alignment, News Impact, and Ethics.

A domain is only cited when its `ResearchField.status` indicates real
evidence exists (never `NOT_COMPUTED`/`NO_EVIDENCE`) -- the same
"never fabricate, never silently promote unavailable to available"
discipline `build_evidence_payload` already applies to `StockResearch`
categories.
"""

from dataclasses import dataclass, field

from alpha_lab.research.ai_rating import AIEvidenceItem, build_evidence_payload
from alpha_lab.research_state import ResearchState

_UNAVAILABLE_STATUSES = frozenset({"NOT_COMPUTED", "NO_EVIDENCE", "NOT_APPLICABLE"})


@dataclass
class ResearchStateEvidence:
    """`build_evidence_payload_from_research_state`'s return: the bounded
    evidence list a provider is allowed to see, plus a traceability map
    from each evidence_id to the `ResearchField.provenance_id` it came
    from -- this is what lets a persisted `AIResearchAssessment` answer
    "which exact evidence did this cite, and where did that evidence come
    from" (see `AIResearchAssessment.evidence_provenance`).

    `provenance` intentionally does not cover every evidence_id: items
    inherited unchanged from the existing `build_evidence_payload` (the
    `fundamental:*`/`metric:*`/`analyst:*`/`technical:*`/`fund:*`/
    `analyst_events:*`/`estimate_revision:*` families) are not given a
    fabricated provenance here -- `ResearchState` does not carry a
    dedicated field for Fund Evidence, and mapping every fundamental
    metric to the coarse `fundamentals` field's own `provenance_id` would
    overstate the precision of that association. Only the domains this
    adapter newly contributes (macro/donatien/alignment/news) get a real,
    precise provenance entry, since those map 1:1 to one `ResearchField`
    each.
    """

    items: list[AIEvidenceItem] = field(default_factory=list)
    provenance: dict[str, str | None] = field(default_factory=dict)


def build_evidence_payload_from_research_state(state: ResearchState) -> ResearchStateEvidence:
    """The Phase 6 evidence adapter: `ResearchState` in, the same bounded
    `AIEvidenceItem` payload `SupplementalResearchService.
    refresh_ai_research_assessment` already builds, widened with
    macro/donatien/alignment/news evidence -- plus precise provenance for
    the newly added items. Reads only fields already on `state`; makes no
    network calls, no database reads, no writes."""
    research = state.stock_research
    items: list[AIEvidenceItem] = list(
        build_evidence_payload(
            categories=research.categories if research is not None else {},
            analyst_consensus=research.analyst_consensus if research is not None else None,
            technical_summary=research.technical_summary if research is not None else None,
            analyst_research=research.analyst_research if research is not None else None,
            fund_evidence=research.fund_evidence if research is not None else None,
        )
    )
    provenance: dict[str, str | None] = {}

    if state.macro.status not in _UNAVAILABLE_STATUSES and state.macro.value is not None:
        macro = state.macro.value
        macro_item = AIEvidenceItem(
            evidence_id="macro:regime",
            description=(
                f"Macro regime = {macro.get('regime')} "
                f"(confidence {_pct(macro.get('confidence'))}, coverage {_pct(macro.get('coverage'))})"
            ),
            source="Macro Regime",
            value=macro.get("regime"),
        )
        items.append(macro_item)
        provenance[macro_item.evidence_id] = state.macro.provenance_id

    if state.donatien.status not in _UNAVAILABLE_STATUSES and state.donatien.value is not None:
        donatien = state.donatien.value
        donatien_item = AIEvidenceItem(
            evidence_id="donatien:dominant_regime",
            description=f"Donatien dominant regime = {donatien.get('dominant_regime')}",
            source="Donatien",
            value=donatien.get("dominant_regime"),
        )
        items.append(donatien_item)
        provenance[donatien_item.evidence_id] = state.donatien.provenance_id

    if state.donatien_alignment.status not in _UNAVAILABLE_STATUSES and state.donatien_alignment.value is not None:
        alignment = state.donatien_alignment.value
        alignment_item = AIEvidenceItem(
            evidence_id="alignment:overall",
            description=f"Macro/Donatien alignment = {alignment.get('alignment')}",
            source="Donatien Alignment",
            value=alignment.get("alignment"),
        )
        items.append(alignment_item)
        provenance[alignment_item.evidence_id] = state.donatien_alignment.provenance_id

    if state.ethics.status not in _UNAVAILABLE_STATUSES and state.ethics.value is not None:
        ethics = state.ethics.value
        ethics_item = AIEvidenceItem(
            evidence_id="ethics:status",
            description=f"Ethical status = {ethics.get('ethical_status')}",
            source="Ethics",
            value=ethics.get("ethical_status"),
        )
        items.append(ethics_item)
        provenance[ethics_item.evidence_id] = state.ethics.provenance_id

    if state.news_impact.status not in _UNAVAILABLE_STATUSES and state.news_impact.value:
        classified = [tag for impact in state.news_impact.value for tag in impact.get("tags", [])]
        categories = sorted({tag["category"] for tag in classified})
        if categories:
            classified_count = sum(1 for impact in state.news_impact.value if impact.get("is_classified"))
            news_item = AIEvidenceItem(
                evidence_id="news:impact_categories",
                description=(
                    f"Recent news impact categories: {', '.join(categories)} "
                    f"({classified_count} classified article(s))"
                ),
                source="News Impact",
            )
            items.append(news_item)
            provenance[news_item.evidence_id] = state.news_impact.provenance_id

    return ResearchStateEvidence(items=items, provenance=provenance)


def _pct(value: object) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:.0%}"
