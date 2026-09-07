"""AlphaLab Macro Regime -- a deterministic, market-derived regime read.

Separate from Donatien External Calibration: this is AlphaLab's own
computation from market-observable proxy prices (VIX, Treasury yields, USD,
oil, gold) already ingested into AlphaLab's own Price table -- never
official economic data, and never a scoring input. Nothing on this page
touches the fundamental score, Analyst Consensus, Technical Summary, or AI
Research Rating.

Opening this page never calls a provider; it only reads whatever was last
persisted by an explicit refresh (this page's button, or
scripts/refresh_macro_regime.py).
"""

import streamlit as st

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.macro import MacroAssessment, MacroRegimeService
from alpha_lab.providers import YFinanceProvider
from alpha_lab.providers.errors import ProviderError

st.set_page_config(page_title="AlphaLab Macro Regime", layout="wide")
st.title("AlphaLab Macro Regime")
st.caption(
    "A deterministic regime read computed from market-observable proxy "
    "prices already stored by AlphaLab -- not official economic data, and "
    "not an input to the fundamental score, Analyst Consensus, Technical "
    "Summary, or AI Research Rating."
)
st.warning(
    "Market-derived proxies only. VIX/Treasury-yield/USD/oil/gold prices "
    "are not the same thing as official CPI, GDP, or employment data, and "
    "this page never implies otherwise."
)


def _dash(value) -> str:
    return "—" if value is None else str(value)


def _regime_label(regime) -> str:
    return regime.value.replace("_", " ").title()


settings = load_settings()
engine = make_engine(settings.database_url)
create_schema(engine)
service = MacroRegimeService(engine)

current = service.get_current()

if current is None:
    st.info(
        "No macro regime assessment has been computed yet. Use the explicit "
        "refresh below, or run `python scripts/refresh_macro_regime.py`."
    )
else:
    assessment = MacroAssessment.model_validate(current.payload)
    st.caption(f"As of {current.as_of} · computed {current.computed_at} UTC")

    columns = st.columns(3)
    columns[0].metric("Regime", _regime_label(assessment.regime))
    columns[1].metric(
        "Confidence", f"{assessment.confidence:.0%}",
        "Regime score: " + (_dash(assessment.regime_score) if assessment.regime_score is None
                             else f"{assessment.regime_score:+.2f}"),
    )
    columns[2].metric("Coverage", f"{assessment.coverage:.0%}")
    if assessment.regime.value == "REVIEW":
        st.caption(
            "REVIEW: neither of the two regime-classifying indicators "
            "(VIX, yield curve spread) was available -- not forced to a "
            "directional read."
        )

    st.subheader("Indicators")
    st.caption(
        "Regime is derived only from Volatility and the yield-curve "
        "spread (textbook risk on/off signals). US Dollar / Oil / Gold "
        "are informational trend context, not folded into the regime "
        "score -- their relationship to risk regime isn't a single "
        "well-established direction."
    )
    st.dataframe(
        [
            {
                "Indicator": indicator.name,
                "Category": indicator.category.value,
                "Ticker(s)": indicator.ticker,
                "Value": _dash(None if indicator.value is None else round(indicator.value, 3)),
                "Signal": _dash(indicator.signal),
                "As of": _dash(indicator.as_of),
                "Status": "AVAILABLE" if indicator.value is not None else "UNAVAILABLE",
            }
            for indicator in assessment.indicators
        ],
        width="stretch",
        hide_index=True,
    )

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
                    "Regime": row.regime,
                    "Score": _dash(row.regime_score),
                    "Coverage": f"{row.coverage:.0%}",
                    "Content hash": row.content_hash[:12],
                }
                for row in history
            ],
            width="stretch",
            hide_index=True,
        )

st.divider()
st.subheader("Refresh macro regime")
st.caption(
    "Opening this page never fetches market data. Only this explicit "
    "action ingests fresh proxy prices and recomputes the regime."
)
if st.button("🔄 Refresh macro regime", key="refresh_macro"):
    with st.spinner("Ingesting proxy prices and recomputing..."):
        try:
            service.refresh(YFinanceProvider())
        except ProviderError as error:
            st.error(f"Macro regime not refreshed: {error.kind.value} — {error.reason}")
        else:
            st.success("Refresh complete — reload the page to see the updated regime above.")
