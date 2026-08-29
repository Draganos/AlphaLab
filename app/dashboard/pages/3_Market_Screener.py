"""Phase 3 market discovery page."""

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
    records = MarketScreenerService(engine, settings).build_live_records()
    query = st.text_input(
        "Natural-language pull search",
        placeholder="Find Sharia-preferred semiconductor companies with strong growth and score above 75",
    )
    criteria = interpret_query(query) if query else ScreenCriteria()
    statuses = st.multiselect(
        "Sharia screening status",
        ["PASS", "REVIEW", "EXCLUDED", "UNKNOWN"],
        default=criteria.ethical_status,
    )
    criteria.ethical_status = statuses
    if query:
        st.markdown("**Interpreted structured filters**")
        st.json(criteria.model_dump())
        if criteria.unsupported:
            st.error(
                "Unsupported / insufficient data: " + ", ".join(criteria.unsupported)
            )
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
            debt_to_ebitda=item.raw_metrics.get("debt_ebitda"),
            market_cap=item.market_cap,
            coverage=item.overall_live_coverage,
        )
        for item in records
    ]
    selected = apply_screen(screen_records, criteria)
    indexed = {item.ticker: item for item in records}
    rows = []
    for item in selected:
        detail = indexed[item.ticker]
        rows.append(
            {
                "Ticker": item.ticker,
                "Company": item.company_name,
                "Price": detail.price,
                "Market Cap": item.market_cap,
                "Sector": item.sector,
                "Industry": item.industry,
                "Overall Rating": item.overall_score,
                "Growth": detail.category_scores.get("earnings_growth"),
                "Revisions": detail.category_scores.get("analyst_revisions"),
                "Quality": detail.category_scores.get("business_quality"),
                "Valuation": detail.category_scores.get("valuation"),
                "Momentum": detail.category_scores.get("momentum"),
                "Financial Strength": detail.category_scores.get("financial_strength"),
                "AI Rating": detail.category_scores.get("ai_research"),
                "Shareholder Return": detail.category_scores.get("shareholder_return"),
                "Coverage": detail.overall_live_coverage,
                "Confidence": detail.confidence,
                "Sharia Status": detail.ethical_status,
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    with st.expander("Saved screeners"):
        repository = Phase3Repository(engine)
        name = st.text_input("Name")
        if st.button("Save current structured screen") and name:
            repository.save_screener(name, criteria.model_dump())
            st.success(f"Saved {name}")
        for saved in repository.list_screeners():
            st.code(f"{saved.name}: {saved.criteria}")
finally:
    engine.dispose()
