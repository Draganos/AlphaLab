"""AlphaLab Research Evidence & Coverage Dashboard (PR #28).

Answers "how much evidence do I actually have for this security?" by
reading the coverage/evidence-count/freshness figures every research
domain already computes -- fundamental categories, Analyst Consensus,
Analyst History, Revisions, Technical, AI Evidence, News, and Macro Regime
-- into one table, per security or across the whole universe. See
`alpha_lab.evidence_coverage.summary` for the read-model this page renders.

This page performs no scoring and computes no new composite/overall score;
it never calls a provider. Opening it only reads whatever research/News/
Macro Regime has already been persisted by other explicit refresh actions
elsewhere in the app.
"""

import pandas as pd
import streamlit as st

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.evidence_coverage import (
    CoverageStatus,
    build_security_coverage_summary,
    flatten_coverage_rows,
    summarize_universe_breakdown,
)
from alpha_lab.macro import MacroRegimeService
from alpha_lab.news import NewsService
from alpha_lab.research import ResearchService

st.set_page_config(page_title="AlphaLab Evidence & Coverage", layout="wide")
st.title("Research Evidence & Coverage")
st.caption(
    "How much evidence AlphaLab actually has per security and category -- "
    "not a score, not a rating. Missing evidence is shown as missing, "
    "never as zero, neutral, or fabricated."
)


def _dash(value) -> str:
    return "—" if value is None else str(value)


def _coverage_label(coverage: float | None) -> str:
    return "—" if coverage is None else f"{coverage:.0%}"


_STATUS_ORDER = {
    CoverageStatus.NOT_COMPUTED: 0,
    CoverageStatus.NO_EVIDENCE: 1,
    CoverageStatus.PARTIAL: 2,
    CoverageStatus.FULL: 3,
}


def _summary_to_rows(summary) -> list[dict]:
    return [
        {
            "Category": row.label,
            "Coverage": _coverage_label(row.coverage),
            "Status": row.status.value,
            "Evidence count": _dash(row.evidence_count),
            "Freshness": _dash(row.freshness),
            "Provider(s)": ", ".join(row.providers) if row.providers else "—",
            "Limitation": _dash(row.limitation_reason),
            "_status_order": _STATUS_ORDER[row.status],
        }
        for row in summary.rows
    ]


@st.cache_data(ttl=300, show_spinner="Loading universe evidence coverage...")
def _load_universe_coverage_rows(
    _research_service: ResearchService,
    _news_service: NewsService,
    _macro_assessment,
    tickers: tuple[str, ...],
) -> list[dict]:
    """Cached so switching the 'Breakdown by' selection (a pure client-side
    regroup of already-fetched rows) never re-triggers the underlying
    2*len(tickers) database reads -- only a genuinely new universe
    (different tickers) or the TTL expiring does. Leading-underscore
    parameters are excluded from Streamlit's cache-key hashing since
    ResearchService/NewsService/MacroAssessment are not hashable."""
    summaries = []
    for ticker in tickers:
        research = _research_service.get_stock_research(ticker)
        if research is None:
            continue
        news_articles = _news_service.get_history(ticker)
        summaries.append(
            build_security_coverage_summary(
                research, news_articles=news_articles, macro_assessment=_macro_assessment
            )
        )
    return flatten_coverage_rows(summaries)


settings = load_settings()
engine = make_engine(settings.database_url)
create_schema(engine)

research_service = ResearchService(engine, settings)
news_service = NewsService(engine)
macro_service = MacroRegimeService(engine)

quotes = research_service.list_current_research()
if not quotes:
    st.info(
        "No persisted current research build exists. Run "
        "`python scripts/rebuild_research.py` after loading attributable data."
    )
    st.stop()

macro_assessment = macro_service.get_current_assessment()

security_tab, universe_tab = st.tabs(["Security Detail", "Universe Breakdown"])

with security_tab:
    ticker = st.selectbox("Security", [quote.ticker for quote in quotes], key="coverage_ticker")
    research = research_service.get_stock_research(ticker)
    if research is None:
        st.info("No research is available for this ticker in the current build.")
        st.stop()
    news_articles = news_service.get_history(ticker)
    summary = build_security_coverage_summary(
        research, news_articles=news_articles, macro_assessment=macro_assessment
    )
    st.caption(
        f"{summary.ticker} · {_dash(summary.sector)} · {_dash(summary.security_type)} "
        f"· as of {summary.evaluation_date}"
    )

    weak_rows = [row for row in summary.rows if row.status != CoverageStatus.FULL]
    columns = st.columns(2)
    columns[0].metric("Categories with full evidence", f"{len(summary.rows) - len(weak_rows)}/{len(summary.rows)}")
    columns[1].metric("Weak or missing categories", str(len(weak_rows)))

    table_rows = _summary_to_rows(summary)
    frame = pd.DataFrame(table_rows).sort_values("_status_order").drop(columns=["_status_order"])
    st.dataframe(frame, width="stretch", hide_index=True)

    if weak_rows:
        st.markdown("**Weak or missing category detail**")
        for row in sorted(weak_rows, key=lambda r: _STATUS_ORDER[r.status]):
            st.caption(
                f"**{row.label}** ({row.status.value}, coverage {_coverage_label(row.coverage)}) "
                f"-- {row.limitation_reason or 'no further detail available'}"
            )
    else:
        st.success("Every tracked category has full evidence coverage for this security.")

with universe_tab:
    st.caption(
        "Computed across every security in the current research build. Only "
        "the Provider breakdown counts each contributing provider "
        "separately -- every other breakdown counts one observation per "
        "security x category, regardless of how many providers it cites."
    )
    flat_rows = _load_universe_coverage_rows(
        research_service, news_service, macro_assessment, tuple(quote.ticker for quote in quotes)
    )

    if not flat_rows:
        st.info("No securities have computed research in the current build.")
        st.stop()

    breakdown_by = st.selectbox(
        "Breakdown by", ["Category", "Security", "Security type", "Sector", "Provider"]
    )
    group_column = {
        "Category": "category",
        "Security": "ticker",
        "Security type": "security_type",
        "Sector": "sector",
        "Provider": "provider",
    }[breakdown_by]

    grouped = pd.DataFrame(summarize_universe_breakdown(flat_rows, group_column))
    if breakdown_by == "Category":
        # Display the human label, not the raw category key -- built from
        # the same flat rows, never a second source of truth for the name.
        category_labels = {row["category"]: row["label"] for row in flat_rows}
        grouped[group_column] = grouped[group_column].map(category_labels)
    grouped = grouped.rename(columns={group_column: breakdown_by})
    grouped["avg_coverage"] = grouped["avg_coverage"].map(lambda v: f"{v:.0%}" if pd.notnull(v) else "—")
    grouped[breakdown_by] = grouped[breakdown_by].fillna("—")
    st.dataframe(grouped, width="stretch", hide_index=True)
