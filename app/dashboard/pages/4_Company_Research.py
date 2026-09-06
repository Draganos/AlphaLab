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


def _render_stock_research(research, *, quote=None) -> None:
    """Render one StockResearch object. Shared, read-only rendering for both
    the current-research view and a selected historical snapshot's detail —
    the same evidence-first presentation either way, never re-derived per
    caller. `quote` (price/market cap/Sharia status/last refresh) is only
    available for current research; historical snapshots don't carry it,
    since it was never part of the canonical StockResearch contract."""
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
    if quote is not None:
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


def _render_history_list(history) -> None:
    st.dataframe(
        [
            {
                "Evaluation date": entry.evaluation_date,
                "Score": "—" if entry.overall_score is None else f"{entry.overall_score:.1f}/100",
                "Coverage": f"{entry.overall_coverage:.0%}",
                "Confidence": f"{entry.confidence:.1f}/10 ({entry.confidence_label})",
                "Rating version": entry.rating_version,
                "Configuration": entry.configuration_hash,
                "Snapshot ID": entry.snapshot_id[:12],
            }
            for entry in history
        ],
        width="stretch",
        hide_index=True,
    )


def _snapshot_option_label(entry) -> str:
    score = "—" if entry.overall_score is None else f"{entry.overall_score:.1f}/100"
    return f"{entry.evaluation_date} · score {score} · {entry.snapshot_id[:8]}"


def _render_comparison(comparison) -> None:
    st.caption(
        f"Comparing {comparison.older_evaluation_date} → {comparison.newer_evaluation_date}. "
        "This shows what changed, not why — evidence, not causal explanation."
    )
    if comparison.rating_version_changed or comparison.configuration_changed:
        st.warning(
            "Rating version or configuration differs between these two snapshots — "
            "differences below may reflect a methodology change, not only new evidence."
        )
    st.dataframe(
        [
            {
                "Metric": "Overall score",
                "Older": _dash(comparison.overall_score_old),
                "Newer": _dash(comparison.overall_score_new),
                "Changed": comparison.overall_score_changed,
            },
            {
                "Metric": "Coverage",
                "Older": f"{comparison.overall_coverage_old:.0%}",
                "Newer": f"{comparison.overall_coverage_new:.0%}",
                "Changed": comparison.overall_coverage_changed,
            },
            {
                "Metric": "Confidence",
                "Older": f"{comparison.confidence_old:.1f}/10",
                "Newer": f"{comparison.confidence_new:.1f}/10",
                "Changed": comparison.confidence_changed,
            },
        ],
        width="stretch",
        hide_index=True,
    )
    st.subheader("Category changes")
    for category in comparison.categories:
        if not (category.score_changed or category.coverage_changed or category.status_changed or category.metric_changes):
            continue
        with st.expander(
            f"{category.label}: {category.old_status.value} → {category.new_status.value}"
        ):
            st.write(
                {
                    "Score": f"{_dash(category.old_score)} → {_dash(category.new_score)}",
                    "Coverage": f"{category.old_coverage:.0%} → {category.new_coverage:.0%}",
                    "Status": f"{category.old_status.value} → {category.new_status.value}",
                }
            )
            if category.metric_changes:
                st.dataframe(
                    [
                        {
                            "Metric": change.metric,
                            "Change": change.change_type.value,
                            "Older": _dash(change.old_value),
                            "Newer": _dash(change.new_value),
                            "Status": f"{change.old_status.value} → {change.new_status.value}",
                        }
                        for change in category.metric_changes
                    ],
                    width="stretch",
                    hide_index=True,
                )
    if not any(
        category.score_changed or category.coverage_changed or category.status_changed or category.metric_changes
        for category in comparison.categories
    ):
        st.info("No category-level changes between these two snapshots.")


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

    _render_stock_research(research, quote=quote)

    st.divider()
    st.subheader("Save this research as a historical snapshot")
    st.caption(
        "Opening this page, changing the ticker, or viewing history never "
        "writes to storage. Only this explicit action persists an immutable "
        "snapshot of the research currently shown above."
    )
    if st.button("💾 Save research snapshot", key="save_snapshot"):
        try:
            saved = service.persist_snapshot(research)
        except Exception as error:  # noqa: BLE001 - surfaced to the user, not swallowed
            st.error(f"Snapshot was NOT persisted — storage failure: {error}")
        else:
            st.success(f"Snapshot persisted (id {saved.snapshot_id[:12]}…).")

    st.divider()
    st.subheader("Research history")
    history = service.get_research_history(ticker)
    if not history:
        st.info(
            "No research snapshots have been saved yet for this ticker. "
            "Use \"Save research snapshot\" above to start building history."
        )
    else:
        _render_history_list(history)

        st.markdown("**View a historical snapshot**")
        selected_label = st.selectbox(
            "Snapshot",
            [_snapshot_option_label(entry) for entry in history],
            key="history_detail_select",
        )
        selected_entry = history[
            [_snapshot_option_label(entry) for entry in history].index(selected_label)
        ]
        with st.expander(f"Historical snapshot detail — {selected_entry.evaluation_date}", expanded=False):
            st.caption(
                "Read-only: this is the persisted snapshot exactly as recorded, "
                "not rebuilt from current data."
            )
            snapshot_research = service.get_research_snapshot(selected_entry.snapshot_id)
            if snapshot_research is None:
                st.error("This snapshot could not be loaded.")
            else:
                _render_stock_research(snapshot_research)

        if len(history) >= 2:
            st.markdown("**Compare two snapshots**")
            options = [_snapshot_option_label(entry) for entry in history]
            older_label = st.selectbox("Older snapshot", options, index=len(options) - 1, key="compare_older")
            newer_label = st.selectbox("Newer snapshot", options, index=0, key="compare_newer")
            older_entry = history[options.index(older_label)]
            newer_entry = history[options.index(newer_label)]
            if older_entry.snapshot_id == newer_entry.snapshot_id:
                st.info("Select two different snapshots to compare.")
            else:
                comparison = service.compare_snapshots(
                    older_entry.snapshot_id, newer_entry.snapshot_id
                )
                if comparison is None:
                    st.error("One of the selected snapshots could not be loaded.")
                else:
                    _render_comparison(comparison)

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
