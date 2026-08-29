from pydantic import ValidationError
import pytest

from alpha_lab.ai import (
    AIResearchResult,
    DeterministicAIResearchProvider,
    analyze_documents,
)
from alpha_lab.search import ScreenRecord, apply_screen, interpret_query


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
