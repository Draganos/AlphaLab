"""Phase 3 market discovery page with manual and validated natural-language filters."""

import pandas as pd
import streamlit as st

from alpha_lab.config import load_settings
from alpha_lab.database import make_engine
from alpha_lab.phase3 import Phase3Repository
from alpha_lab.screener import MarketScreenerService
from alpha_lab.search import ScreenCriteria, ScreenRecord, apply_screen, interpret_query

st.set_page_config(page_title="AlphaLab Market Screener", layout="wide")
st.title("Market Screener")
st.caption(
    "AlphaLab is an investment research system. Scores are research signals, not guaranteed predictions."
)
st.warning(
    "Sharia-preferred screening is a configurable research filter and is not a religious ruling or formal certification."
)
settings = load_settings()
engine = make_engine(settings.database_url)
try:
    repository = Phase3Repository(engine)
    saved = repository.list_screeners()
    with st.expander("Saved screeners", expanded=False):
        names = [item.name for item in saved]
        selected_saved = st.selectbox("Saved definition", [""] + names)
        c1, c2, c3 = st.columns(3)
        if c1.button("Load") and selected_saved:
            payload = next(
                item.criteria for item in saved if item.name == selected_saved
            )
            for key in list(st.session_state):
                del st.session_state[key]
            st.session_state["active_screen"] = payload
            st.rerun()
        rename = c2.text_input("Rename to")
        if c2.button("Rename") and selected_saved and rename:
            repository.rename_screener(selected_saved, rename)
            st.rerun()
        if c3.button("Delete") and selected_saved:
            repository.delete_screener(selected_saved)
            st.rerun()
    loaded = ScreenCriteria.model_validate(st.session_state.get("active_screen", {}))
    query = st.text_input(
        "Natural-language pull search",
        placeholder="Find Sharia-preferred semiconductor companies with strong growth, low debt and score above 75",
    )
    criteria = interpret_query(query) if query else loaded
    records = MarketScreenerService(engine, settings).build_live_records()
    sectors = sorted({item.sector for item in records if item.sector})
    industries = sorted({item.industry for item in records if item.industry})
    exchanges = sorted({item.exchange for item in records if item.exchange})
    themes = sorted({theme for item in records for theme in item.themes})
    st.subheader("Structured filters")
    sectors = sorted(set(sectors) | set(criteria.sectors))
    industries = sorted(set(industries) | set(criteria.industries))
    exchanges = sorted(set(exchanges) | set(criteria.exchanges))
    themes = sorted(set(themes) | set(criteria.themes))
    row1 = st.columns(4)
    criteria.text_search = (
        row1[0].text_input("Company / ticker", value=criteria.text_search or "") or None
    )
    criteria.sectors = row1[1].multiselect(
        "Sector", sectors, default=criteria.sectors
    )
    criteria.industries = row1[2].multiselect(
        "Industry",
        industries,
        default=criteria.industries,
    )
    criteria.exchanges = row1[3].multiselect(
        "Exchange", exchanges, default=criteria.exchanges
    )
    row2 = st.columns(4)
    criteria.themes = row2[0].multiselect(
        "Theme", themes, default=criteria.themes
    )
    criteria.ethical_status = row2[1].multiselect(
        "Sharia status",
        ["PASS", "REVIEW", "EXCLUDED", "UNKNOWN"],
        default=criteria.ethical_status or ["PASS"],
    )
    criteria.minimum_overall_score = (
        row2[2].number_input(
            "Minimum overall rating",
            0.0,
            100.0,
            float(criteria.minimum_overall_score or 0),
        )
        or None
    )
    criteria.minimum_coverage = row2[3].number_input(
        "Minimum coverage",
        0.0,
        1.0,
        float(
            criteria.minimum_coverage
            if criteria.minimum_coverage is not None
            else settings.strategy.minimum_data_coverage
        ),
        0.05,
    )
    score_fields = [
        ("Growth", "minimum_growth_score"),
        ("Revisions", "minimum_revisions_score"),
        ("Quality", "minimum_quality_score"),
        ("Valuation", "minimum_valuation_score"),
        ("Momentum", "minimum_momentum_score"),
        ("Financial strength", "minimum_financial_strength_score"),
        ("AI rating", "minimum_ai_research_score"),
        ("Shareholder return", "minimum_shareholder_return_score"),
    ]
    columns = st.columns(4)
    for index, (label, field) in enumerate(score_fields):
        value = columns[index % 4].number_input(
            f"Minimum {label}",
            0.0,
            100.0,
            float(getattr(criteria, field) or 0),
            key=field,
        )
        setattr(criteria, field, value or None)
    row4 = st.columns(3)
    criteria.minimum_market_cap = (
        row4[0].number_input(
            "Minimum market cap",
            0.0,
            value=float(criteria.minimum_market_cap or 0),
            step=1e8,
        )
        or None
    )
    criteria.maximum_market_cap = (
        row4[1].number_input(
            "Maximum market cap (0 = none)",
            0.0,
            value=float(criteria.maximum_market_cap or 0),
            step=1e8,
        )
        or None
    )
    criteria.maximum_debt_to_ebitda = (
        row4[2].number_input(
            "Maximum debt / EBITDA (0 = none)",
            0.0,
            value=float(criteria.maximum_debt_to_ebitda or 0),
            step=0.25,
        )
        or None
    )
    st.session_state["active_screen"] = criteria.model_dump()
    if query:
        st.json(criteria.model_dump())
    if criteria.unsupported:
        st.error("Unsupported / insufficient data: " + ", ".join(criteria.unsupported))
    screen_records = [
        ScreenRecord(
            ticker=item.ticker,
            company_name=item.company,
            country=item.country,
            exchange=item.exchange,
            sector=item.sector,
            industry=item.industry,
            themes=item.themes,
            ethical_status=item.ethical_status,
            overall_score=item.overall_score,
            growth_score=item.category_scores.get("earnings_growth"),
            revisions_score=item.category_scores.get("analyst_revisions"),
            quality_score=item.category_scores.get("business_quality"),
            valuation_score=item.category_scores.get("valuation"),
            momentum_score=item.category_scores.get("momentum"),
            financial_strength_score=item.category_scores.get("financial_strength"),
            ai_research_score=item.category_scores.get("ai_research"),
            shareholder_return_score=item.category_scores.get("shareholder_return"),
            debt_to_ebitda=item.raw_metrics.get("debt_ebitda"),
            market_cap=item.market_cap,
            coverage=item.overall_live_coverage,
        )
        for item in records
    ]
    selected = apply_screen(screen_records, criteria)
    indexed = {item.ticker: item for item in records}
    rows = [
        {
            "Ticker": item.ticker,
            "Company": item.company_name,
            "Price": indexed[item.ticker].price,
            "Market Cap": item.market_cap,
            "Sector": item.sector,
            "Industry": item.industry,
            "Overall Rating": item.overall_score,
            "Growth": item.growth_score,
            "Revisions": item.revisions_score,
            "Quality": item.quality_score,
            "Valuation": item.valuation_score,
            "Momentum": item.momentum_score,
            "Financial Strength": item.financial_strength_score,
            "AI Rating": item.ai_research_score,
            "Shareholder Return": item.shareholder_return_score,
            "Coverage": item.coverage,
            "Data Quality": indexed[item.ticker].data_quality_status,
            "Sharia Status": item.ethical_status,
        }
        for item in selected
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    with st.expander("Save current screen"):
        name = st.text_input("Screen name")
        if st.button("Save") and name:
            repository.save_screener(name, criteria.model_dump())
            st.success(f"Saved {name}")
finally:
    engine.dispose()
