from pydantic import ValidationError
import pytest

from alpha_lab.ai import (
    AIResearchResult,
    DeterministicAIResearchProvider,
    analyze_documents,
)
from alpha_lab.search import (
    DeterministicQueryInterpreter,
    ScreenCriteria,
    ScreenRecord,
    apply_screen,
    interpret_query,
)


def test_ai_is_structured_evidence_bearing_and_optional():
    result = analyze_documents(
        DeterministicAIResearchProvider(),
        "TECH",
        [{"id": 7, "text": "Strong demand and raised guidance."}],
    )
    assert result is not None and result.ai_rating > 50
    assert result.evidence[0].document_id == 7
    assert analyze_documents(None, "TECH", []) is None
    payload = result.model_dump()
    payload["price_target"] = 100
    with pytest.raises(ValidationError):
        AIResearchResult.model_validate(payload)


def test_natural_language_becomes_filters_not_tickers():
    criteria = interpret_query(
        "Find Sharia-preferred semiconductor companies with strong growth and score above 75"
    )
    assert criteria.themes == ["semiconductors"]
    assert criteria.minimum_overall_score == 75
    records = [
        ScreenRecord(
            ticker="A",
            ethical_status="PASS",
            themes=["semiconductors"],
            overall_score=80,
            growth_score=80,
            coverage=0.9,
        ),
        ScreenRecord(
            ticker="BANK",
            ethical_status="EXCLUDED",
            themes=["semiconductors"],
            overall_score=99,
            growth_score=99,
            coverage=1,
        ),
    ]
    assert [item.ticker for item in apply_screen(records, criteria)] == ["A"]
    assert interpret_query("similar to Nvidia with insider buying").unsupported


def test_unimplemented_raw_metric_comparison_is_explicitly_unsupported():
    criteria = DeterministicQueryInterpreter().interpret(
        "Find stocks with P/E below 10"
    )
    assert criteria.unsupported == ["P/E below 10"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "Find profitable semiconductor companies with strong growth, low debt and a score above 75",
            {
                "minimum_quality_score": 55,
                "minimum_growth_score": 70,
                "maximum_debt_to_ebitda": 2,
                "minimum_overall_score": 75,
            },
        ),
        (
            "Find Sharia-preferred US stocks with strong AI demand commentary and positive earnings revisions",
            {
                "minimum_ai_research_score": 70,
                "minimum_revisions_score": 55,
                "countries": ["US"],
            },
        ),
        (
            "Find undervalued healthcare companies with improving margins",
            {"minimum_valuation_score": 70, "minimum_quality_score": 70},
        ),
        (
            "Find companies benefiting from data-centre investment",
            {"themes": ["data centres"]},
        ),
        (
            "Show companies similar to Nvidia but trading at cheaper valuations",
            {"minimum_valuation_score": 70},
        ),
        (
            "Find airlines with improving fundamentals",
            {"minimum_growth_score": 60, "themes": ["aviation"]},
        ),
    ],
)
def test_phase3_example_queries_map_conditions(query, expected):
    criteria = DeterministicQueryInterpreter().interpret(query)
    for field, value in expected.items():
        assert getattr(criteria, field) == value
    if "similar to" in query.casefold():
        assert "similar-company matching" in criteria.unsupported


def test_optional_live_providers_default_closed(monkeypatch):
    from alpha_lab.ai import configured_ai_research_provider
    from alpha_lab.search import configured_query_interpreter

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "openai")
    monkeypatch.setenv("ALPHALAB_QUERY_PROVIDER", "openai")
    assert configured_ai_research_provider() is None
    assert isinstance(configured_query_interpreter(), DeterministicQueryInterpreter)


def test_all_structured_category_filters_execute_deterministically():
    criteria = ScreenCriteria(
        minimum_coverage=0.7,
        minimum_revisions_score=60,
        minimum_quality_score=60,
        minimum_valuation_score=60,
        minimum_momentum_score=60,
        minimum_financial_strength_score=60,
        minimum_ai_research_score=60,
        minimum_shareholder_return_score=60,
    )
    complete = ScreenRecord(
        ticker="COMPLETE",
        ethical_status="PASS",
        coverage=0.9,
        revisions_score=70,
        quality_score=70,
        valuation_score=70,
        momentum_score=70,
        financial_strength_score=70,
        ai_research_score=70,
        shareholder_return_score=70,
    )
    missing = complete.model_copy(
        update={"ticker": "MISSING", "ai_research_score": None}
    )
    assert [item.ticker for item in apply_screen([missing, complete], criteria)] == [
        "COMPLETE"
    ]


def test_live_ai_provider_retains_only_supplied_evidence(monkeypatch):
    import json
    from alpha_lab.ai import OpenAIResearchProvider, analyze_documents

    payload = {
        "guidance_score": 1,
        "demand_score": 1,
        "margin_outlook_score": 0,
        "competitive_position_score": 1,
        "management_confidence_score": 1,
        "balance_sheet_commentary_score": 0,
        "risk_score": 0,
        "sentiment_score": 1,
        "catalyst_score": 0,
        "key_positives": ["Demand"],
        "key_risks": [],
        "evidence": [{"document_id": 7, "excerpt": "Attributable excerpt"}],
        "summary": "Evidence-only analysis",
        "provider": "model",
        "model": "model",
        "prompt_version": "phase3-v1",
        "confidence": 0.8,
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps(payload)}}]}
            ).encode()

    monkeypatch.setattr(
        "alpha_lab.ai.research.urlopen", lambda *args, **kwargs: Response()
    )
    provider = OpenAIResearchProvider("test-key")
    result = provider.analyze("X", [{"id": 7, "text": "Source document"}])
    assert result.evidence[0].document_id == 7
    payload["evidence"][0]["document_id"] = 99
    assert (
        analyze_documents(provider, "X", [{"id": 7, "text": "Source document"}]) is None
    )
