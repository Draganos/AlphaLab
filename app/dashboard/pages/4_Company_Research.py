"""Evidence-oriented company drill-down."""

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import make_engine
from alpha_lab.database.models import AIResearchAnalysis, EthicalEvaluation
from alpha_lab.screener import MarketScreenerService

st.set_page_config(page_title="AlphaLab Company Research", layout="wide")
st.title("Company Research")
st.caption("Research signals are not guaranteed predictions or investment advice.")
st.warning(
    "Sharia-preferred screening is a configurable research filter—not a religious ruling or formal certification."
)
settings = load_settings()
engine = make_engine(settings.database_url)
try:
    records = MarketScreenerService(engine, settings).build_live_records()
    if not records:
        st.info("Load attributable company data before using this page.")
        st.stop()
    ticker = st.selectbox("Company", [item.ticker for item in records])
    item = next(value for value in records if value.ticker == ticker)
    st.header(f"{item.company or ticker} · {ticker}")
    columns = st.columns(4)
    columns[0].metric(
        "Overall rating",
        "Unavailable"
        if item.overall_score is None
        else f"{item.overall_score:.1f}/100",
    )
    columns[1].metric("Live coverage", f"{item.overall_live_coverage:.0%}")
    columns[2].metric("Confidence", item.confidence)
    columns[3].metric("Sharia status", item.ethical_status)
    st.write(
        {
            "Price": item.price,
            "Market cap": item.market_cap,
            "Sector": item.sector,
            "Industry": item.industry,
            "Last refreshed": item.last_refreshed,
        }
    )
    st.subheader("Transparent category scorecards")
    for category, score in item.category_scores.items():
        with st.expander(
            f"{category.replace('_', ' ').title()} — {'Unavailable' if score is None else f'{score:.1f}/100'}"
        ):
            st.json(item.raw_metrics)
    with Session(engine) as session:
        ethics = session.scalar(
            select(EthicalEvaluation)
            .where(EthicalEvaluation.ticker == ticker)
            .order_by(EthicalEvaluation.evaluated_at.desc())
        )
        ai = session.scalar(
            select(AIResearchAnalysis)
            .where(AIResearchAnalysis.ticker == ticker)
            .order_by(AIResearchAnalysis.analysis_date.desc())
        )
        st.subheader("Sharia-preferred / values card")
        if ethics:
            st.json(
                {
                    "status": ethics.ethical_status,
                    "primary_business": ethics.primary_business,
                    "business_tags": ethics.business_tags,
                    "hard_exclusions": ethics.exclusion_reasons,
                    "review_reasons": ethics.review_reasons,
                    "financial_warnings": ethics.financial_warnings,
                    "why": ethics.evidence,
                    "source": ethics.source,
                    "policy_version": ethics.policy_version,
                }
            )
        else:
            st.info(
                "UNKNOWN — no attributable business classification has been evaluated."
            )
        st.subheader("AI research evidence")
        if ai:
            st.metric("AI research", f"{ai.ai_rating:.1f}/100")
            st.json(
                {
                    "components": ai.component_scores,
                    "key_positives": ai.key_positives,
                    "key_risks": ai.key_risks,
                    "evidence": ai.evidence,
                    "provider": ai.provider,
                    "model": ai.model,
                    "prompt_version": ai.prompt_version,
                    "confidence": ai.confidence,
                }
            )
        else:
            st.info(
                "AI research unavailable. Missing AI remains missing and does not block the application."
            )
    st.subheader("Provenance")
    st.json(item.provenance)
finally:
    engine.dispose()
