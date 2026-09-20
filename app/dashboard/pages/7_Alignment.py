"""Phase 2B: Donatien <-> Market Regime Alignment -- a small, categorical
comparison of AlphaLab Macro Regime and Donatien External Calibration.

Purely categorical: ALIGNED / CONFLICT / NEUTRAL / INSUFFICIENT_DATA. There
is no alignment score, conviction score, or hidden weighting anywhere on
this page, and nothing here touches the fundamental score, Analyst
Consensus, Technical Summary, AI Research Rating, or ranking. See
alpha_lab.alignment.alignment's module docstring for the exact
deterministic mapping.

Opening this page never calls a provider or the network; it only reads
whatever Market Regime / Donatien evidence is already stored, and
recomputes the categorical comparison from it. Use the explicit "Recompute
alignment" action below (or scripts/refresh_alignment.py) after refreshing
Macro Regime and/or External Calibration on their own pages.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import streamlit as st

from alpha_lab.alignment import Alignment, AlignmentAssessment, AlignmentService
from alpha_lab.calibration import ExternalCalibrationService
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.macro import MacroRegimeService

st.set_page_config(page_title="AlphaLab Alignment", layout="wide")
st.title("Donatien ↔ Market Regime Alignment")
st.caption(
    "Compares two independent, already-computed evidence layers -- "
    "AlphaLab Macro Regime (market-derived proxies) and Donatien External "
    "Calibration (third-party) -- and states only whether they agree, "
    "disagree, or cannot be compared. No score, no ranking impact."
)
st.warning(
    "Categorical only. This page never produces a numeric alignment/"
    "conviction score, and has zero influence on AlphaLab's fundamental "
    "score, Analyst Consensus, Technical Summary, AI Research Rating, "
    "ranking, or portfolio weights."
)

_ALIGNMENT_COLOR = {
    Alignment.ALIGNED.value: "🟢",
    Alignment.CONFLICT.value: "🔴",
    Alignment.NEUTRAL.value: "🟡",
    Alignment.INSUFFICIENT_DATA.value: "⚪",
}


def _dash(value) -> str:
    return "—" if value is None else str(value)


def _label(value) -> str:
    return _dash(value).replace("_", " ").title()


def _render_assessment(assessment: AlignmentAssessment) -> None:
    icon = _ALIGNMENT_COLOR.get(assessment.alignment.value, "")
    st.metric("Alignment", f"{icon} {_label(assessment.alignment.value)}")

    columns = st.columns(2)
    with columns[0]:
        st.markdown("**Market Regime**")
        st.write(f"Regime: {_label(assessment.market_regime.value if assessment.market_regime else None)}")
        st.write(
            "Coverage: "
            + ("—" if assessment.market_regime_coverage is None else f"{assessment.market_regime_coverage:.0%}")
        )
        st.write(f"As of: {_dash(assessment.market_as_of)}")
    with columns[1]:
        st.markdown("**Donatien External Calibration**")
        st.write(
            "Lean (from scenario_weights): "
            + _label(assessment.donatien_lean.value if assessment.donatien_lean else None)
        )
        st.caption(
            "Audit-only context below is never used to derive the lean above "
            "-- Donatien's dominant_regime is free text and defensiveness "
            "has no documented scale (see architecture docs)."
        )
        st.write(f"Dominant regime (audit-only): {_dash(assessment.donatien_dominant_regime)}")
        st.write(f"Confidence (audit-only): {_dash(assessment.donatien_confidence)}")
        st.write(f"Defensiveness (audit-only): {_dash(assessment.donatien_defensiveness)}")
        st.write(f"Run date: {_dash(assessment.donatien_run_date)}")

    if assessment.donatien_scenario_weights:
        st.markdown("**Scenario weights (the structured field that drives the lean)**")
        st.write(assessment.donatien_scenario_weights)

    if assessment.alignment == Alignment.INSUFFICIENT_DATA:
        reasons = []
        if assessment.market_regime is None:
            reasons.append("no Market Regime evidence available for this date")
        elif assessment.market_regime.value == "REVIEW":
            reasons.append("Market Regime itself is REVIEW (no basis to judge)")
        if assessment.donatien_lean is None:
            reasons.append("no Donatien Calibration evidence available for this date")
        elif assessment.donatien_lean.value == "UNKNOWN":
            reasons.append("Donatien's scenario_weights used an unrecognized taxonomy")
        if reasons:
            st.info("Why INSUFFICIENT_DATA: " + "; ".join(reasons) + ".")


def _staleness_reasons(assessment: AlignmentAssessment, engine) -> list[str]:
    """Pure database reads (no network, no write) comparing this stored
    assessment's own recorded inputs against whatever Macro Regime /
    Donatien Calibration is currently stored -- safe to call on every
    render, exactly like every other read in this codebase. Each of those
    two pages requires its own explicit refresh, and this page's own
    "Recompute alignment" only re-reads what is already stored (see this
    page's own docstring/caption) -- so a refresh on either of those pages
    leaves this page showing a stale comparison until it is explicitly
    told to recompute. This never recomputes anything by itself; it only
    makes that already-real gap visible instead of silent."""
    reasons = []
    current_macro = MacroRegimeService(engine).get_current()
    if current_macro is not None and (
        assessment.market_as_of is None or current_macro.as_of > assessment.market_as_of
    ):
        reasons.append(
            f"Macro Regime has newer evidence (as of {current_macro.as_of}) than this "
            f"alignment used (as of {_dash(assessment.market_as_of)})"
        )
    current_calibration = ExternalCalibrationService(engine).get_current()
    if current_calibration is not None and (
        assessment.donatien_retrieved_at is None
        or current_calibration.retrieved_at > assessment.donatien_retrieved_at
    ):
        reasons.append(
            f"Donatien External Calibration has a newer retrieval "
            f"({current_calibration.retrieved_at} UTC) than this alignment used "
            f"({_dash(assessment.donatien_retrieved_at)})"
        )
    return reasons


settings = load_settings()
engine = make_engine(settings.database_url)
create_schema(engine)
service = AlignmentService(engine)

current = service.get_current()

if current is None:
    st.info(
        "No alignment has been computed yet. Use the explicit recompute "
        "below, or run `python scripts/refresh_alignment.py`."
    )
else:
    assessment = AlignmentAssessment.model_validate(current.payload)
    st.caption(f"As of {current.as_of} · computed {current.computed_at} UTC")
    stale_reasons = _staleness_reasons(assessment, engine)
    if stale_reasons:
        st.warning(
            "This alignment is stale relative to newer evidence already stored: "
            + "; ".join(stale_reasons)
            + '. Click "Recompute alignment" below to update it -- refreshing '
            "Macro Regime or External Calibration never does this automatically."
        )
    _render_assessment(assessment)

    with st.expander("Raw alignment payload (audit view)"):
        st.json(current.payload)

    st.subheader("History")
    history = service.get_history()
    if not history:
        st.info("No historical snapshots yet.")
    else:
        st.dataframe(
            [
                {
                    "Computed (UTC)": row.created_at,
                    "As of": row.as_of,
                    "Alignment": row.alignment,
                    "Market regime": AlignmentAssessment.model_validate(row.payload).market_regime,
                    "Donatien lean": AlignmentAssessment.model_validate(row.payload).donatien_lean,
                    "Content hash": row.content_hash[:12],
                }
                for row in history
            ],
            width="stretch",
            hide_index=True,
        )

st.divider()
st.subheader("Recompute alignment")
st.caption(
    "Opening this page never fetches data. This action only re-reads "
    "whatever Market Regime / Donatien evidence is already stored and "
    "recomputes the comparison -- refresh those two pages first if you "
    "want this to reflect new evidence."
)
if st.button("🔄 Recompute alignment", key="refresh_alignment"):
    with st.spinner("Recomputing alignment from stored evidence..."):
        service.refresh()
    st.success("Recompute complete — reload the page to see the updated alignment above.")
