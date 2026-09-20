"""Evidence-oriented company drill-down, driven by the canonical StockResearch object."""

from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.alignment import AlignmentService
from alpha_lab.calibration.sector_alignment import get_sector_tier_weights_for_ticker
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import AIResearchAnalysis, EthicalEvaluation
from alpha_lab.news import NewsService
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.providers import YFinanceProvider
from alpha_lab.providers.errors import ProviderError
from alpha_lab.research import CATEGORY_LABELS, CATEGORY_ORDER, ResearchService
from alpha_lab.research.ai_rating import DIMENSION_NAMES
from alpha_lab.research.analyst_events import AnalystEventsService
from alpha_lab.research.supplemental_service import SupplementalResearchService
from alpha_lab.research.technical import IndicatorCategory
from alpha_lab.research_stance import ResearchStance, build_research_stance

st.set_page_config(page_title="AlphaLab Company Research", layout="wide")
st.title("Company Research")
st.caption("Research signals are not guaranteed predictions or investment advice.")
st.warning(
    "Sharia-preferred screening is a configurable research filter—not a religious ruling or formal certification."
)


def _dash(value) -> str:
    """Never render None as 0/blank/False — an explicit placeholder instead."""
    return "—" if value is None else str(value)


def _rating_label(rating) -> str:
    return rating.value.replace("_", " ").title()


_DIMENSION_LABELS = {
    "business_outlook": "Business Outlook",
    "growth_prospects": "Growth Prospects",
    "competitive_position": "Competitive Position",
    "valuation_context": "Valuation Context",
    "risk_profile": "Risk Profile",
    "catalyst_strength": "Catalyst Strength",
}

_INDICATOR_SIGNAL_LABELS = {1: "Buy", 0: "Neutral", -1: "Sell"}


def _indicator_signal_label(signal: int | None) -> str:
    """UNAVAILABLE != NEUTRAL: an indicator with no signal is never shown as Neutral."""
    return "Unavailable" if signal is None else _INDICATOR_SIGNAL_LABELS[signal]


def _distribution_bar(label: str, count: int | None, max_count: int, width: int = 12) -> str:
    """One line of a monospace distribution bar. A missing count renders as
    '—', never as an empty/zero bar, so absence stays visually distinct from
    a confirmed zero."""
    if count is None:
        return f"{label:<11} —"
    filled = round((count / max_count) * width) if max_count > 0 else 0
    return f"{label:<11} {'█' * filled}{' ' * (width - filled)} {count}"


def _render_analyst_consensus_panel(column, consensus) -> None:
    column.markdown("**Analyst Consensus**")
    if consensus is None:
        column.caption("Not yet computed for this ticker.")
        return
    column.markdown(f"### {_rating_label(consensus.rating)}")
    if consensus.rating.value == "REVIEW":
        column.caption("Insufficient or ambiguous recommendation data for a safe consensus.")
    column.write(
        {
            "Strong Buy": _dash(consensus.strong_buy),
            "Buy": _dash(consensus.buy),
            "Hold": _dash(consensus.hold),
            "Sell": _dash(consensus.sell),
            "Strong Sell": _dash(consensus.strong_sell),
        }
    )
    counts = (
        ("Strong Buy", consensus.strong_buy),
        ("Buy", consensus.buy),
        ("Hold", consensus.hold),
        ("Sell", consensus.sell),
        ("Strong Sell", consensus.strong_sell),
    )
    max_count = max((count for _, count in counts if count is not None), default=0)
    column.code("\n".join(_distribution_bar(label, count, max_count) for label, count in counts))
    column.caption(f"{_dash(consensus.total_analysts)} analyst(s)")
    price_targets = (
        ("Current", consensus.target_current),
        ("Average", consensus.target_mean),
        ("Low", consensus.target_low),
        ("High", consensus.target_high),
    )
    if any(value is not None for _, value in price_targets):
        column.markdown("**Price Target**")
        column.write({label: _dash(None if value is None else f"${value:,.2f}") for label, value in price_targets})
    if consensus.upside_to_mean is not None:
        column.caption(f"Implied upside: {consensus.upside_to_mean:+.1%}")
    column.caption(f"Coverage {consensus.coverage:.0%} · source {consensus.source} ({consensus.as_of})")


def _render_technical_summary_panel(column, technical) -> None:
    column.markdown("**Technical Summary**")
    if technical is None:
        column.caption("Not yet computed for this ticker.")
        return
    column.markdown(f"### {_rating_label(technical.overall_rating)}")
    if technical.overall_rating.value == "REVIEW":
        column.caption("Insufficient indicator coverage for a safe rating.")
    column.write(
        {
            "Moving averages": f"{_rating_label(technical.moving_average_rating)} "
            f"({technical.moving_average_available}/{technical.moving_average_total})",
            "Oscillators": f"{_rating_label(technical.oscillator_rating)} "
            f"({technical.oscillator_available}/{technical.oscillator_total})",
        }
    )
    total = technical.moving_average_total + technical.oscillator_total
    available = technical.moving_average_available + technical.oscillator_available
    column.caption(
        f"Coverage {available}/{total} ({technical.coverage:.1%}) · "
        f"{technical.timeframe.value} · {technical.as_of}"
    )
    if technical.indicator_agreement is not None:
        column.caption(
            f"Indicator agreement: {_rating_label(technical.indicator_agreement)} — "
            f"{technical.buy_signal_count} Buy · {technical.sell_signal_count} Sell · "
            f"{technical.neutral_signal_count} Neutral (of {available} available)"
        )
    with column.expander("Indicators"):
        moving_averages = [
            indicator for indicator in technical.indicators
            if indicator.category == IndicatorCategory.MOVING_AVERAGE
        ]
        oscillators = [
            indicator for indicator in technical.indicators
            if indicator.category == IndicatorCategory.OSCILLATOR
        ]

        def _indicator_rows(indicators):
            return [
                {
                    "Indicator": indicator.name,
                    "Value": _dash(None if indicator.value is None else round(indicator.value, 2)),
                    "Signal": _indicator_signal_label(indicator.signal),
                }
                for indicator in indicators
            ]

        st.markdown("**Moving Averages**")
        st.dataframe(_indicator_rows(moving_averages), width="stretch", hide_index=True)
        st.markdown("**Oscillators**")
        st.dataframe(_indicator_rows(oscillators), width="stretch", hide_index=True)


def _render_ai_research_panel(column, assessment) -> None:
    column.markdown("**AI Research Rating**")
    if assessment is None:
        column.caption("Not yet computed for this ticker.")
        return
    score_label = "Unavailable" if assessment.score is None else f"{assessment.score:.0f} / 100"
    column.markdown(f"### {score_label}")
    column.caption(_rating_label(assessment.rating))
    if assessment.rating.value == "REVIEW":
        column.caption("Insufficient evidence for a reliable directional assessment.")
        if assessment.evidence_gaps:
            column.caption("Reasons: " + "; ".join(assessment.evidence_gaps))
    column.caption(f"Confidence: {assessment.confidence:.0%}")
    column.caption(f"Evidence referenced: {len(assessment.supporting_evidence)}")
    if assessment.evidence_gaps:
        column.caption(f"Evidence gaps: {len(assessment.evidence_gaps)}")
    coverage = assessment.evidence_coverage
    domain_2_label = "Fund" if coverage.fund_coverage is not None else "Analyst"
    domain_2_value = coverage.fund_coverage if coverage.fund_coverage is not None else coverage.analyst_coverage
    column.caption(
        "AI evidence coverage — Fundamental "
        f"{coverage.fundamental_coverage:.0%} · {domain_2_label} {domain_2_value:.0%} · "
        f"Technical {coverage.technical_coverage:.0%} · Overall {coverage.overall_ai_evidence_coverage:.0%}"
    )
    column.write(
        {
            _DIMENSION_LABELS[name]: _rating_label(assessment.dimensions[name].value)
            for name in DIMENSION_NAMES
        }
    )
    with column.expander("Evidence used"):
        if assessment.supporting_evidence:
            st.write(assessment.supporting_evidence)
        else:
            st.caption("No evidence was cited by the provider.")


def _render_fund_evidence_panel(research) -> None:
    """PR #30: holdings/sector/asset-class/operations evidence for a fund
    (e.g. an ETF) -- the evidence that exists *instead* of the six
    fundamental categories alpha_lab.research.security_type marks
    NOT_APPLICABLE for one. Renders nothing for an equity (fund_evidence is
    always None there) so this never adds noise to a non-fund ticker's page."""
    evidence = research.fund_evidence
    if evidence is None:
        return
    st.subheader("Fund Evidence")
    st.caption(
        "Holdings, sector/asset-class allocation, and fund operations -- "
        "the evidence AlphaLab actually has for a fund, rather than judging "
        "it against the equity fundamental-scoring categories above/below "
        "(most of which are NOT_APPLICABLE to a fund; see Category overview)."
    )
    identity = f"{_dash(evidence.category_name)} · {_dash(evidence.fund_family)} · {_dash(evidence.legal_type)}"
    st.caption(identity)
    columns = st.columns(4)
    columns[0].metric(
        "Expense ratio",
        _dash(None if evidence.operations.expense_ratio is None else f"{evidence.operations.expense_ratio:.2%}"),
        None
        if evidence.operations.category_avg_expense_ratio is None
        else f"category avg {evidence.operations.category_avg_expense_ratio:.2%}",
    )
    columns[1].metric(
        "Total net assets",
        _dash(None if evidence.operations.total_net_assets is None else f"{evidence.operations.total_net_assets:,.0f}"),
    )
    columns[2].metric(
        "Holdings turnover",
        _dash(None if evidence.operations.holdings_turnover is None else f"{evidence.operations.holdings_turnover:.0%}"),
    )
    columns[3].metric(
        "Top holdings concentration",
        _dash(
            None
            if evidence.top_holdings_concentration is None
            else f"{evidence.top_holdings_concentration:.0%}"
        ),
        f"of {len(evidence.top_holdings)} holdings" if evidence.top_holdings else None,
    )
    st.caption(f"Fund evidence coverage: {evidence.coverage:.0%}")

    detail_columns = st.columns(3)
    detail_columns[0].markdown("**Asset allocation**")
    allocation = evidence.asset_allocation
    detail_columns[0].write(
        {
            "Cash": _dash(None if allocation.cash is None else f"{allocation.cash:.1%}"),
            "Stock": _dash(None if allocation.stock is None else f"{allocation.stock:.1%}"),
            "Bond": _dash(None if allocation.bond is None else f"{allocation.bond:.1%}"),
            "Preferred": _dash(None if allocation.preferred is None else f"{allocation.preferred:.1%}"),
            "Convertible": _dash(None if allocation.convertible is None else f"{allocation.convertible:.1%}"),
            "Other": _dash(None if allocation.other is None else f"{allocation.other:.1%}"),
        }
    )
    detail_columns[1].markdown("**Sector weightings**")
    if evidence.sector_weightings:
        top_sectors = sorted(evidence.sector_weightings.items(), key=lambda item: item[1], reverse=True)
        detail_columns[1].write({sector: f"{weight:.1%}" for sector, weight in top_sectors if weight})
    else:
        detail_columns[1].caption("Not reported.")
    detail_columns[2].markdown("**Equity holdings averages**")
    eh = evidence.equity_holdings
    detail_columns[2].write(
        {
            "P/E": _dash(eh.price_earnings),
            "P/B": _dash(eh.price_book),
            "P/S": _dash(eh.price_sales),
            "P/CF": _dash(eh.price_cashflow),
        }
    )

    if evidence.top_holdings:
        with st.expander(f"Top {len(evidence.top_holdings)} holdings"):
            st.dataframe(
                [
                    {
                        "Symbol": _dash(holding.symbol),
                        "Name": _dash(holding.name),
                        "Weight": _dash(None if holding.weight is None else f"{holding.weight:.2%}"),
                    }
                    for holding in evidence.top_holdings
                ],
                width="stretch",
                hide_index=True,
            )
    if evidence.description:
        with st.expander("Fund description"):
            st.caption(evidence.description)


def _render_supplemental_panels(research) -> None:
    """Analyst Consensus / Technical Summary / AI Research Rating -- three
    research outputs separate from the AlphaLab fundamental score above.
    Never implies these contribute to that score; each renders
    independently and any of the three can be unavailable on its own."""
    st.caption(
        "Separate research outputs, not inputs to the AlphaLab Fundamental "
        "Score above — each is independent and may be unavailable on its own. "
        "'AI Research Rating' here is a cross-domain synthesis, distinct from "
        "the fundamental 'AI Research' category and 'AI research evidence' "
        "section further below on this page."
    )
    columns = st.columns(3)
    _render_analyst_consensus_panel(columns[0], research.analyst_consensus)
    _render_technical_summary_panel(columns[1], research.technical_summary)
    _render_ai_research_panel(columns[2], research.ai_research_assessment)


_OUTCOME_DISPLAY = {
    "POSITIVE": "Positive",
    "MIXED_POSITIVE": "Mixed — leaning positive",
    "NEUTRAL": "Neutral / evenly mixed",
    "MIXED_NEGATIVE": "Mixed — leaning negative",
    "NEGATIVE": "Negative",
    "INSUFFICIENT_DATA": "Insufficient data",
}


def _render_research_stance_panel(research, stance: "ResearchStance | None") -> None:
    """PR #31: cross-domain Research Stance — an inspectable, traceable
    synthesis of already-computed evidence, never a new composite score.
    See `alpha_lab.research_stance`'s module docstring for the full
    methodology, including exactly which domains are directional and why
    News/Coverage-confidence deliberately never are.

    `stance` is `None` only for a historical snapshot view: Macro Regime/
    External Calibration/News would otherwise be read "as of now" rather
    than as of the snapshot's own evaluation date, leaking information a
    point-in-time-correct view must not have — see
    `alpha_lab.research_stance`'s docstring and the project roadmap's PR
    #32 (historical validation), which owns fixing this properly. For a
    snapshot, this still computes a stance from the snapshot's own frozen
    fundamentals/analyst/revisions/technical/AI-research fields (all
    genuinely point-in-time), with Macro/External Calibration/News simply
    reading NOT_COMPUTED rather than a leaked current-day read.
    """
    if stance is None:
        stance = build_research_stance(research)
        st.caption(
            "Historical snapshot: Macro Regime, External Calibration, and "
            "News reflect this snapshot's own evidence only (not computed "
            "here), never a current-day read leaking into the past."
        )
    st.subheader("Research Stance")
    st.caption(
        "An inspectable synthesis of already-computed evidence layers — "
        "never a new composite score, and never a replacement for the "
        "AlphaLab Fundamental Score below. Every line traces back to an "
        "existing research domain's own vocabulary."
    )
    st.metric("Research stance", _OUTCOME_DISPLAY[stance.outcome.value])
    if stance.conflicts:
        st.warning(
            "Evidence conflicts detected — not hidden inside the stance above:\n\n"
            + "\n".join(f"- {conflict}" for conflict in stance.conflicts)
        )
    rows = [
        {
            "Domain": line.label_display,
            "Assessment": line.label.replace("_", " ").title(),
            "Detail": line.detail or "",
        }
        for line in stance.lines
    ]
    st.dataframe(rows, width="stretch", hide_index=True)


def _render_stock_research(research, *, quote=None, stance: "ResearchStance | None" = None) -> None:
    """Render one StockResearch object. Shared, read-only rendering for both
    the current-research view and a selected historical snapshot's detail —
    the same evidence-first presentation either way, never re-derived per
    caller. `quote` (price/market cap/Sharia status/last refresh) is only
    available for current research; historical snapshots don't carry it,
    since it was never part of the canonical StockResearch contract.
    `stance` (PR #31) is likewise only computed with full context (Macro/
    External Calibration/News) for the current-research view — see
    `_render_research_stance_panel`'s docstring for why a historical
    snapshot deliberately omits it instead of leaking a current-day read."""
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

    st.divider()
    _render_supplemental_panels(research)
    st.divider()
    _render_fund_evidence_panel(research)
    if research.fund_evidence is not None:
        st.divider()
    _render_research_stance_panel(research, stance)
    st.divider()
    st.subheader("AlphaLab Fundamental Research")
    st.caption(
        "The section below is the existing quantitative AlphaLab research "
        "system. Analyst Consensus, Technical Summary, and AI Research "
        "above are separate outputs and never change this score."
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
        "coverage · AVAILABLE = full evidence and a score · NOT_APPLICABLE "
        "= this category does not apply to this security's type (e.g. "
        "valuation for an ETF). Missing evidence is never treated as a "
        "negative signal, and neither is a category that was never "
        "expected to apply."
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


_ACTION_LABELS = {
    "main": "Reiteration",
    "init": "Initiation",
    "up": "Upgrade",
    "down": "Downgrade",
    "reit": "Reiteration",
}


def _rating_change_action_label(action: str | None) -> str:
    if action is None:
        return "—"
    return _ACTION_LABELS.get(action, action.title())


_DIRECTION_LABELS = {
    "IMPROVING": "Improving",
    "DETERIORATING": "Deteriorating",
    "STABLE": "Stable",
    "REVIEW": "Review (insufficient data)",
}


def _render_rating_changes_table(analyst_research) -> None:
    st.markdown("**Analyst Rating Changes**")
    st.caption(
        "Discrete upgrade/downgrade/initiation/reiteration events, each "
        "with its own real historical date — the evidence behind the "
        "Analyst Consensus rating above, not a new score. Not a scoring "
        "input anywhere in AlphaLab."
    )
    if analyst_research is None or not analyst_research.recent_rating_changes:
        st.info(
            "No analyst rating-change history has been refreshed yet for "
            "this ticker, or this instrument has no analyst coverage "
            "(e.g. most ETFs)."
        )
        return
    counts = analyst_research.rating_change_counts_90d
    if counts is not None:
        st.caption(
            f"Last 90 days: {counts['upgrades']} upgrade(s), "
            f"{counts['downgrades']} downgrade(s), {counts['initiations']} "
            f"initiation(s), {counts['reiterations']} reiteration(s)."
        )
    st.dataframe(
        [
            {
                "Date": event.grade_date,
                "Firm": _dash(event.firm),
                "Action": _rating_change_action_label(event.action),
                "To": _dash(event.to_grade),
                "From": _dash(event.from_grade),
                "Price target": _dash(
                    None
                    if event.current_price_target is None
                    else f"${event.current_price_target:,.2f}"
                ),
                "Prior target": _dash(
                    None
                    if event.prior_price_target is None
                    else f"${event.prior_price_target:,.2f}"
                ),
            }
            for event in analyst_research.recent_rating_changes
        ],
        width="stretch",
        hide_index=True,
    )


def _render_revision_trend_table(analyst_research) -> None:
    st.markdown("**Estimate Revision Trend**")
    st.caption(
        "The source's own already-computed EPS-consensus trend and "
        "analyst up/down revision counts, by fiscal period — distinct "
        "from the Analyst Revisions category further below on this page "
        "(which is a scored fundamental-research input derived "
        "differently, from AlphaLab's own accumulated estimate history). "
        "This trend evidence is never a scoring input."
    )
    if analyst_research is None or not analyst_research.revision_trend:
        st.info(
            "No estimate revision trend has been refreshed yet for this "
            "ticker, or this instrument has no analyst estimate coverage."
        )
        return
    st.dataframe(
        [
            {
                "Fiscal period": row.fiscal_period,
                "Current EPS est.": _dash(row.eps_trend_current),
                "7d ago": _dash(row.eps_trend_7d_ago),
                "30d ago": _dash(row.eps_trend_30d_ago),
                "60d ago": _dash(row.eps_trend_60d_ago),
                "90d ago": _dash(row.eps_trend_90d_ago),
                "Direction (vs. 30d ago)": _DIRECTION_LABELS.get(
                    row.direction.value, row.direction.value
                ),
                "Analysts revising up (7d/30d)": f"{_dash(row.revisions_up_last_7d)} / {_dash(row.revisions_up_last_30d)}",
                "Analysts revising down (7d/30d)": f"{_dash(row.revisions_down_last_7d)} / {_dash(row.revisions_down_last_30d)}",
                "As of": row.observation_date,
            }
            for row in analyst_research.revision_trend
        ],
        width="stretch",
        hide_index=True,
    )


def _render_history_list(history) -> None:
    """Shows both dates deliberately: `evaluation_date` is the date the
    underlying evidence applies to (set once per screener rebuild, so two
    snapshots saved hours apart on the same day can share it); `Snapshot
    saved` (`created_at`) is when AlphaLab actually persisted this exact
    row. Showing only evaluation_date previously made same-day snapshots
    look identical/stuck even when their content genuinely differed —
    that was the root cause of the "wrong date" reports, not a bad value
    in either field. See regression test
    test_history_list_rows_distinguish_same_day_snapshots_by_created_at."""
    st.dataframe(
        [
            {
                "Evaluation date": entry.evaluation_date,
                "Snapshot saved": entry.created_at,
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
    saved = entry.created_at.strftime("%Y-%m-%d %H:%M:%S")
    return f"{entry.evaluation_date} (saved {saved}) · score {score} · {entry.snapshot_id[:8]}"


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
create_schema(engine)
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

    stance_news_articles = NewsService(engine).get_history(ticker, limit=20)
    stance_alignment = AlignmentService(engine).get_current_assessment()
    stance = build_research_stance(
        research, news_articles=stance_news_articles, alignment=stance_alignment
    )
    _render_stock_research(research, quote=quote, stance=stance)

    st.divider()
    st.subheader("Donatien External Calibration — sector context (audit-only)")
    st.caption(
        "Which of Donatien's own published tier weight lines reference this "
        "security's sector, and at exactly the weight Donatien published — "
        "not a new score, not an alignment verdict, and not a ranking "
        "input. The sector match uses a best-effort Morningstar-to-GICS "
        "name correspondence (AlphaLab's `sector` field is Morningstar's "
        "taxonomy via yfinance, not verified GICS), so treat this as "
        "informational context, never as certified sector classification."
    )
    sector_weights = get_sector_tier_weights_for_ticker(engine, ticker)
    if not sector_weights:
        st.info(
            "No Donatien tier weight line matches this security's sector "
            "(or no calibration/sector data is available)."
        )
    else:
        st.dataframe(
            [
                {
                    "Tier": row.tier,
                    "Matched GICS sector": row.gics_sector,
                    "Weight %": row.pct,
                    "Vehicle": row.vehicle,
                    "Line": row.line_name,
                }
                for row in sector_weights
            ],
            width="stretch",
            hide_index=True,
        )

    st.divider()
    st.subheader("News (evidence only)")
    st.caption(
        "Stored news articles for this ticker, exactly as reported — no "
        "sentiment, no relevance score, no NewsImpact classification, and "
        "no effect on the Alpha score, ranking, or portfolio weights. "
        "Opening this page or changing the ticker never fetches news; only "
        "the explicit refresh below does."
    )
    news_service = NewsService(engine)
    articles = news_service.get_history(ticker, limit=20)
    if not articles:
        st.info(
            "No news has been refreshed yet for this ticker. Use the "
            "explicit refresh below, or run "
            "`python scripts/refresh_news.py " + ticker + "`."
        )
    else:
        st.dataframe(
            [
                {
                    "Published": row.published_at,
                    "Title": row.title,
                    "Publisher": _dash(row.publisher),
                    "URL": row.url,
                    "Retrieved (UTC)": row.retrieved_at,
                }
                for row in articles
            ],
            width="stretch",
            hide_index=True,
        )
    if st.button("🔄 Refresh news for this ticker", key="refresh_news"):
        with st.spinner(f"Refreshing news for {ticker}..."):
            try:
                result = news_service.refresh(YFinanceProvider(), ticker)
            except ProviderError as error:
                st.error(f"News not refreshed: {error.kind.value} — {error.reason}")
            else:
                st.success(
                    f"Refresh complete — fetched {result.fetched}, stored "
                    f"{result.stored} new, {result.duplicates} already known, "
                    f"{result.invalid} invalid/skipped. Reload the page to see "
                    "the updated list above."
                )

    st.divider()
    st.subheader("Refresh Analyst Consensus, Technical Summary, Fund Evidence & AI Research")
    st.caption(
        "Opening this page or changing the ticker never calls a provider or "
        "recomputes these. Only this explicit action does — Analyst "
        "Consensus and Fund Evidence each make one live yfinance call "
        "(Fund Evidence genuinely returns nothing for an equity, which is "
        "not an error); Technical Summary and AI Research use only "
        "already-stored data."
    )
    if st.button("🔄 Refresh for this ticker", key="refresh_supplemental"):
        supplemental = SupplementalResearchService(engine)
        with st.spinner(f"Refreshing {ticker}..."):
            result = supplemental.refresh_all(ticker, YFinanceProvider(), research)
        if result.analyst_error is not None:
            error = result.analyst_error
            st.warning(f"Analyst Consensus not refreshed: {error.kind.value} — {error.reason}")
            # The AI Research Rating explicitly synthesizes all three
            # domains; refreshing it anyway would silently replace a
            # previously valid assessment with one missing this domain's
            # evidence. See SupplementalResearchService.refresh_all.
            st.warning(
                "AI Research Rating not refreshed — it requires Analyst "
                "Consensus, which failed to refresh above. The previous "
                "AI Research Rating (if any) is unchanged."
            )
        if result.fund_evidence_error is not None:
            error = result.fund_evidence_error
            st.warning(
                f"Fund Evidence not refreshed: {error.kind.value} — {error.reason}. "
                "The AI Research Rating above used the last successfully stored "
                "Fund Evidence, if any."
            )
        st.success("Refresh complete — reload the page to see the updated panels above.")
        # Automatic forward-only historical snapshotting (roadmap: historical
        # research reconstruction) -- see ResearchService.snapshot_current_
        # research's own docstring. scripts/refresh_supplemental_research.py's
        # batch run calls the exact same shared method, so history
        # accumulates identically from either usage path, not just this one.
        try:
            saved = service.snapshot_current_research(ticker)
        except Exception as error:  # noqa: BLE001 - surfaced, not swallowed
            st.warning(f"Automatic historical snapshot was not saved: {error}")
        else:
            if saved is not None:
                st.caption(f"Historical snapshot recorded automatically (id {saved.snapshot_id[:12]}…).")

    st.divider()
    st.subheader("Analyst Rating Changes & Estimate Revision Trend")
    st.caption(
        "Two further evidence layers behind the Analyst Consensus panel "
        "above: the discrete rating-change events analysts actually "
        "issued, and the source's own reported estimate-revision trend. "
        "Neither is a new score, neither is collapsed into Analyst "
        "Consensus, and neither is a scoring input to the AlphaLab "
        "Fundamental Score or the existing Analyst Revisions category "
        "below. Opening this page or changing the ticker never calls a "
        "provider — only the explicit refresh below does."
    )
    rating_left, revision_right = st.columns(2)
    with rating_left:
        _render_rating_changes_table(research.analyst_research)
    with revision_right:
        _render_revision_trend_table(research.analyst_research)
    if st.button("🔄 Refresh rating changes & revision trend", key="refresh_analyst_events"):
        events_service = AnalystEventsService(engine)
        with st.spinner(f"Refreshing analyst events for {ticker}..."):
            outcome = events_service.refresh_all(ticker, YFinanceProvider())
        if outcome.rating_changes_error is not None:
            error = outcome.rating_changes_error
            st.warning(f"Rating changes not refreshed: {error.kind.value} — {error.reason}")
        else:
            st.success(f"Rating changes: {outcome.rating_changes_stored} new event(s) stored.")
        if outcome.revision_trend_error is not None:
            error = outcome.revision_trend_error
            st.warning(f"Revision trend not refreshed: {error.kind.value} — {error.reason}")
        else:
            st.success(
                f"Revision trend: {outcome.revision_trend_stored} new observation(s) stored."
            )
        st.info("Reload the page to see the updated tables above.")

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
        with st.expander(
            f"Historical snapshot detail — evaluation {selected_entry.evaluation_date} "
            f"(saved {selected_entry.created_at.strftime('%Y-%m-%d %H:%M:%S')})",
            expanded=False,
        ):
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

    st.divider()
    st.subheader("Reconstruct research as of a past date")
    st.caption(
        "Not a search across every saved moment: Analyst Consensus/AI "
        "Research Rating/Fund Evidence have no history of their own before "
        "this feature existed, so this finds the most recently *persisted* "
        "research snapshot on or before the chosen date for those three "
        "(see \"Save this research as a historical snapshot\" and the "
        "automatic snapshot recorded by \"Refresh for this ticker\" above). "
        "Technical Summary needs no snapshot at all -- it is always "
        "recomputed exactly as of the chosen date from AlphaLab's own "
        "stored price history."
    )
    as_of_date = st.date_input("As of date", value=date.today(), key="research_as_of_date")
    if st.button("Reconstruct", key="reconstruct_as_of"):
        as_of_snapshot = service.get_latest_snapshot_as_of(ticker, as_of_date)
        as_of_technical = SupplementalResearchService(engine).get_technical_summary_as_of(
            ticker, as_of_date
        )
        if as_of_snapshot is None:
            st.warning(
                f"No research snapshot had been saved for {ticker} by {as_of_date} "
                "yet -- Analyst Consensus/AI Research Rating/Fund Evidence are "
                "unavailable for this date. Showing Technical Summary alone, "
                "reconstructed from stored price history."
            )
            _render_technical_summary_panel(st, as_of_technical)
        else:
            st.caption(
                f"Nearest snapshot on or before {as_of_date}: evaluation "
                f"{as_of_snapshot.evaluation_date}. Technical Summary below is "
                f"recomputed exactly as of {as_of_date}, not the snapshot's own date."
            )
            _render_stock_research(
                as_of_snapshot.model_copy(update={"technical_summary": as_of_technical})
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
