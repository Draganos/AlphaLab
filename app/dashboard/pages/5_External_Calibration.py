"""Donatien External Calibration -- read-only display of a third-party
market regime/scenario snapshot.

EXTERNAL_CALIBRATION, not ground truth, and not a stock-rating engine: this
page never touches StockResearch, the composite score, Analyst Consensus,
Technical Summary, AI Research Rating, or any ranking. It is global market
context, not per-security research -- deliberately absent from the Market
Screener table.

Opening this page never calls the Donatien provider; it only reads whatever
was last persisted by an explicit refresh (this page's button, or
scripts/refresh_donatien_calibration.py).
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import streamlit as st

from alpha_lab.calibration import ExternalCalibrationService
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.providers.donatien import DonatienCalibration, DonatienProvider
from alpha_lab.providers.errors import ProviderError

st.set_page_config(page_title="AlphaLab External Calibration", layout="wide")
st.title("External Calibration / Market Regime")
st.caption(
    "Third-party market regime/scenario data (Donatien), not an AlphaLab "
    "signal. It has zero influence on AlphaLab's fundamental score, Analyst "
    "Consensus, Technical Summary, AI Research Rating, or ranking."
)
st.warning(
    "EXTERNAL_CALIBRATION, not ground truth. This page displays only what "
    "the source actually reported -- nothing here is derived, scored, or "
    "used by AlphaLab in Phase 1."
)


def _dash(value) -> str:
    return "—" if value is None else str(value)


def _render_calibration(calibration: DonatienCalibration) -> None:
    """Every field below except `dominant_regime`/`scenario_weights`/`tiers`
    is Optional on `DonatienCalibration` (see that model's docstring):
    Donatien's live schema changed on 2026-09-14, and this page must render
    correctly under either the original or the current shape -- a field
    genuinely absent from whichever observation this is renders as "not
    reported by source", never as a blank/crashed section and never as a
    fabricated value."""
    st.subheader("Source data")
    left, right = st.columns(2)
    left.markdown(f"**Dominant regime**\n\n{calibration.dominant_regime}")
    right.metric("Confidence", _dash(calibration.confidence))

    st.markdown("**Scenario weights**")
    st.write(calibration.scenario_weights)

    columns = st.columns(2)
    columns[0].metric("Defensiveness", _dash(calibration.defensiveness))
    columns[1].markdown("**Top drivers**")
    if calibration.top_drivers is None:
        columns[1].caption("Not reported by source for this observation.")
    else:
        columns[1].write(
            {driver.name: driver.dominance for driver in calibration.top_drivers}
        )

    st.markdown("**Key changes**")
    if calibration.key_changes is None:
        st.caption("Not reported by source for this observation.")
    else:
        for change in calibration.key_changes:
            st.markdown(f"- {change}")

    st.markdown("**Trend / Contrarian split**")
    if calibration.trend_contrarian_split is None:
        st.caption(
            "Not reported at the top level for this observation "
            "(see per-tier Trend % / Contrarian % below instead)."
        )
    else:
        st.write(calibration.trend_contrarian_split)

    if calibration.macro_report or calibration.note:
        st.markdown("**Source notes**")
        if calibration.macro_report:
            st.caption(f"Referenced report: {calibration.macro_report}")
        if calibration.note:
            st.markdown(calibration.note)

    st.subheader("Tiers")
    for tier_name, tier in calibration.tiers.items():
        with st.expander(tier_name):
            if tier.expected_behaviour:
                st.caption(tier.expected_behaviour)
            if tier.trend_pct is not None or tier.contrarian_pct is not None:
                st.caption(
                    f"Trend % / Contrarian %: {_dash(tier.trend_pct)} / {_dash(tier.contrarian_pct)}"
                )
            rows = [
                {
                    "Line item": name,
                    "Weight %": line.pct,
                    "Asset class": line.asset_class,
                    "Vehicle": line.vehicle,
                    "GICS sector": _dash(line.gics_sector),
                    "Tag": _dash(line.tag),
                }
                for name, line in tier.weights.items()
            ]
            st.dataframe(rows, width="stretch", hide_index=True)


settings = load_settings()
engine = make_engine(settings.database_url)
create_schema(engine)
service = ExternalCalibrationService(engine)

current = service.get_current()

if current is None:
    st.info(
        "No Donatien calibration has been refreshed yet. Use the explicit "
        "refresh below, or run `python scripts/refresh_donatien_calibration.py`."
    )
else:
    st.caption(
        f"Source: {current.source} · "
        f"Source observation: {_dash(current.source_run_date)} {_dash(current.source_run_time_raw)} "
        "(timezone not established by the source) · "
        f"AlphaLab retrieved: {current.retrieved_at} UTC"
    )
    if current.supersedes:
        st.caption(f"Supersedes: {current.supersedes}")
    calibration = DonatienCalibration.model_validate(current.normalized_payload)
    _render_calibration(calibration)

    with st.expander("Raw validated payload (audit view)"):
        st.json(current.raw_payload)

    st.subheader("History")
    history = service.get_history()
    if not history:
        st.info("No historical snapshots yet.")
    else:
        st.dataframe(
            [
                {
                    "Retrieved at (UTC)": row.retrieved_at,
                    "Source observation": f"{_dash(row.source_run_date)} {_dash(row.source_run_time_raw)}",
                    "Dominant regime": DonatienCalibration.model_validate(
                        row.normalized_payload
                    ).dominant_regime,
                    "Content hash": row.content_hash[:12],
                }
                for row in history
            ],
            width="stretch",
            hide_index=True,
        )

st.divider()
st.subheader("Refresh Donatien calibration")
st.caption(
    "Opening this page never calls Donatien. Only this explicit action "
    "makes one live HTTP request. A failed refresh leaves the calibration "
    "above completely unchanged."
)
if st.button("🔄 Refresh calibration", key="refresh_calibration"):
    with st.spinner("Fetching Donatien calibration..."):
        try:
            service.refresh(DonatienProvider(settings.donatien_url))
        except ProviderError as error:
            st.error(f"Calibration not refreshed: {error.kind.value} — {error.reason}")
        else:
            st.success("Refresh complete — reload the page to see the updated calibration above.")
