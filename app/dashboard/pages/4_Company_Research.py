"""Evidence-oriented company drill-down, driven by the canonical StockResearch object."""

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import make_engine
from alpha_lab.database.models import AIResearchAnalysis, EthicalEvaluation
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.research import CATEGORY_LABELS, CATEGORY_ORDER, ResearchService

st.set_page_config(page_title="AlphaLab Company Research", layout="wide")
st.title("Company Research")
st.caption("Research signals are not guaranteed predictions or investment advice.")
st.warning(
    "Sharia-preferred screening is a configurable research filter—not a religious ruling or formal certification."
)


def _dash(value) -> str:
    """Never render None as 0/blank/False — an explicit placeholder instead."""
    return "—" if value is None else str(value)


settings = load_settings()
engine = make_engine(settings.database_url)
try:
    service = ResearchService(engine, settings)
    quotes = service.list_current_research()
    current_build, _ = Phase3Repository(engine).latest_current_payloads()
    if not quotes:
        st.info(
            "No persisted current research build exists. Run "
            "`python scripts/rebuild_research.py` after loading attributable data."
        )
        st.stop()
    ticker = st.selectbox("Company", [quote.ticker for quote in quotes])
    quote = next(value for value in quotes if value.ticker == ticker)
    research = service.get_stock_research(ticker)
    if research is None:
        st.info("No research is available for this ticker in the current build.")
        st.stop()
    if current_build is not None:
        st.caption(
            f"Research rebuilt {current_build.built_at}; evaluation "
            f"{current_build.evaluation_date}; version {current_build.score_version}"
        )

    st.header(f"{research.company_name or research.ticker} · {research.ticker}")
    st.caption(
        f"{_dash(research.sector)} / {_dash(research.industry)} · "
        f"{_dash(research.security_type)} · evaluated {research.evaluation_date}"
    )
    columns = st.columns(4)
    columns[0].metric(
        "Overall score",
        "Unavailable"
        if research.overall_score is None
        else f"{research.overall_score:.1f}/100",
    )
    columns[1].metric("Coverage", f"{research.overall_coverage:.0%}")
    columns[2].metric("Confidence", f"{research.confidence:.1f}/10", research.confidence_label)
    columns[3].metric("Data quality", research.data_quality_status)
    st.caption(
        "Confidence is not the same as Overall Score, and Coverage is not the "
        "same as Confidence — see the confidence breakdown below."
    )
    st.write(
        {
            "Price": quote.price,
            "Market cap": quote.market_cap,
            "Sharia status": quote.ethical_status,
            "Last refreshed": quote.last_refreshed,
        }
    )

    with st.expander("Confidence breakdown"):
        breakdown = research.confidence_breakdown
        st.caption(
            f"Legacy score/coverage label for comparison: {research.score_interpretation}"
        )
        st.dataframe(
            [
                {"Component": "Coverage", "Weight": "50%", "Value": f"{breakdown.overall_coverage:.0%}"},
                {"Component": "Category breadth", "Weight": "20%", "Value": f"{breakdown.category_breadth:.0%}"},
                {"Component": "Freshness", "Weight": "20%", "Value": f"{breakdown.freshness:.0%}"},
                {"Component": "Source quality", "Weight": "10%", "Value": f"{breakdown.source_quality:.0%}"},
            ],
            width="stretch",
            hide_index=True,
        )
        if breakdown.data_quality_penalty_applied:
            st.caption(
                f"A data-quality penalty was applied because data quality is "
                f"'{research.data_quality_status}', not 'valid'."
            )

    st.subheader("Category overview")
    st.caption(
        "UNAVAILABLE = zero evidence · PARTIAL = some evidence, not full "
        "coverage · AVAILABLE = full evidence and a score. Missing evidence "
        "is never treated as a negative signal."
    )
    overview_rows = [
        {
            "Category": category.label,
            "Score": "—" if category.score is None else f"{category.score:.0f}/100",
            "Coverage": f"{category.coverage:.0%}",
            "Status": category.status.value,
        }
        for category in (research.categories[name] for name in CATEGORY_ORDER)
    ]
    st.dataframe(overview_rows, width="stretch", hide_index=True)

    st.subheader("Category detail")
    for name in CATEGORY_ORDER:
        category = research.categories[name]
        header = (
            f"{CATEGORY_LABELS[name]} — "
            f"{'—' if category.score is None else f'{category.score:.0f}/100'} "
            f"· {category.coverage:.0%} coverage · {category.status.value}"
        )
        with st.expander(header):
            if not category.metrics:
                st.info(
                    "No metric-level evidence is modeled for this category yet."
                    if name == "ai_research"
                    else "No metrics are documented for this category."
                )
            else:
                rows = [
                    {
                        "Metric": metric.name,
                        "Value": _dash(metric.value),
                        "Unit": _dash(metric.unit),
                        "Percentile": _dash(
                            None if metric.percentile is None else round(metric.percentile, 1)
                        ),
                        "Status": metric.status.value,
                        "Period": _dash(metric.period),
                        "Source": _dash(metric.source),
                        "Retrieved at": _dash(metric.retrieved_at),
                        "Formula": _dash(metric.formula),
                        "Inputs": _dash(metric.inputs),
                    }
                    for metric in category.metrics
                ]
                st.dataframe(rows, width="stretch", hide_index=True)
            if category.unavailable_metrics:
                st.caption(
                    "Unavailable — no evidence available: "
                    + ", ".join(category.unavailable_metrics)
                )

    st.subheader("Strengths, weaknesses, and risks")
    st.caption(
        "Reproduced from the canonical StockResearch object; not re-derived here."
    )
    left, middle, right = st.columns(3)
    left.markdown("**Strengths**")
    left.write(research.strengths or "—")
    middle.markdown("**Weaknesses**")
    middle.write(research.weaknesses or "—")
    right.markdown("**Risks**")
    right.write(research.risks or "—")

    st.subheader("Catalysts")
    if research.catalysts:
        st.write(research.catalysts)
    else:
        st.info(
            "No catalysts are available. AI Research — UNAVAILABLE: catalyst "
            "identification is a future phase and is never fabricated."
        )

    st.caption("Sources: " + (", ".join(research.sources) if research.sources else "—"))
    st.caption(
        f"Rating version {research.rating_version} · configuration "
        f"{research.configuration_hash} · generated {research.generated_at}"
    )

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
finally:
    engine.dispose()
