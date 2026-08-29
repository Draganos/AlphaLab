"""Structured, evidence-bearing AI analyst layer with an offline deterministic provider."""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any
import re
import json
import os
from urllib.request import Request, urlopen
from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceReference(BaseModel):
    document_id: int
    excerpt: str

    @field_validator("excerpt")
    @classmethod
    def no_price_target(cls, value: str) -> str:
        return _reject_price_target(value)


class AIResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guidance_score: float = Field(ge=-2, le=2)
    demand_score: float = Field(ge=-2, le=2)
    margin_outlook_score: float = Field(ge=-2, le=2)
    competitive_position_score: float = Field(ge=-2, le=2)
    management_confidence_score: float = Field(ge=-2, le=2)
    balance_sheet_commentary_score: float = Field(ge=-2, le=2)
    risk_score: float = Field(ge=-2, le=2)
    sentiment_score: float = Field(ge=-2, le=2)
    catalyst_score: float = Field(ge=-2, le=2)
    key_positives: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    summary: str
    provider: str
    model: str
    prompt_version: str
    confidence: float = Field(ge=0, le=1)
    analysis_date: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("summary")
    @classmethod
    def no_price_target(cls, value: str) -> str:
        return _reject_price_target(value)

    @field_validator("key_positives", "key_risks")
    @classmethod
    def no_price_targets_in_lists(cls, values: list[str]) -> list[str]:
        return [_reject_price_target(value) for value in values]

    @property
    def ai_rating(self) -> float:
        values = [
            self.guidance_score,
            self.demand_score,
            self.margin_outlook_score,
            self.competitive_position_score,
            self.management_confidence_score,
            self.balance_sheet_commentary_score,
            -self.risk_score,
            self.sentiment_score,
            self.catalyst_score,
        ]
        return round((sum(values) / len(values) + 2) / 4 * 100, 2)

    def raw_structured_output(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AIResearchProvider(ABC):
    @abstractmethod
    def analyze(
        self, ticker: str, documents: list[dict[str, Any]]
    ) -> AIResearchResult: ...


class DeterministicAIResearchProvider(AIResearchProvider):
    """Keyword fixture for tests; it makes no external calls and fabricates no documents."""

    positive = (
        "raised guidance",
        "strong demand",
        "margin expansion",
        "market share gain",
        "record backlog",
    )
    negative = (
        "lowered guidance",
        "weak demand",
        "margin pressure",
        "liquidity risk",
        "investigation",
    )

    def analyze(self, ticker: str, documents: list[dict[str, Any]]) -> AIResearchResult:
        text = " ".join(str(document.get("text", "")) for document in documents).lower()
        positive = [phrase for phrase in self.positive if phrase in text]
        negative = [phrase for phrase in self.negative if phrase in text]
        signal = max(-2, min(2, len(positive) - len(negative)))
        evidence = [
            EvidenceReference(
                document_id=int(document["id"]),
                excerpt=str(document.get("text", ""))[:180],
            )
            for document in documents
            if document.get("id") is not None
        ]
        return AIResearchResult(
            guidance_score=signal,
            demand_score=signal,
            margin_outlook_score=signal,
            competitive_position_score=signal,
            management_confidence_score=signal,
            balance_sheet_commentary_score=0,
            risk_score=min(2, len(negative)),
            sentiment_score=signal,
            catalyst_score=signal,
            key_positives=positive,
            key_risks=negative,
            evidence=evidence,
            summary=f"Deterministic evidence analysis for {ticker}; {len(documents)} source document(s).",
            provider="deterministic-fixture",
            model="keyword-v1",
            prompt_version="phase3-v1",
            confidence=min(1.0, len(documents) / 2),
        )


class OpenAIResearchProvider(AIResearchProvider):
    """Optional live analyst using only caller-supplied attributable documents."""

    def __init__(self, api_key: str, model: str = "gpt-4.1-mini"):
        self.api_key, self.model = api_key, model

    def analyze(self, ticker: str, documents: list[dict[str, Any]]) -> AIResearchResult:
        allowed_ids = {
            int(document["id"])
            for document in documents
            if document.get("id") is not None
        }
        sources = [
            {"id": document["id"], "text": str(document.get("text", ""))}
            for document in documents
            if document.get("id") is not None
        ]
        prompt = (
            "Analyze only these attributable documents. Return JSON matching this schema exactly. "
            "Scores are -2 to +2. risk_score is higher for greater risk. Never provide a price target. "
            "Every evidence document_id must be one supplied.\nSchema: "
            + json.dumps(AIResearchResult.model_json_schema())
            + "\nTicker: "
            + ticker
            + "\nDocuments: "
            + json.dumps(sources)
        )
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }
        ).encode()
        request = Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HTTPS endpoint
            body = json.loads(response.read())
        result = AIResearchResult.model_validate_json(
            body["choices"][0]["message"]["content"]
        )
        if any(
            reference.document_id not in allowed_ids for reference in result.evidence
        ):
            raise ValueError("AI returned evidence outside supplied documents")
        return result.model_copy(
            update={
                "provider": "openai",
                "model": self.model,
                "prompt_version": "phase3-live-v1",
            }
        )


def configured_ai_research_provider() -> AIResearchProvider | None:
    if os.getenv(
        "ALPHALAB_AI_PROVIDER", "disabled"
    ).casefold() == "openai" and os.getenv("OPENAI_API_KEY"):
        return OpenAIResearchProvider(
            os.environ["OPENAI_API_KEY"], os.getenv("ALPHALAB_AI_MODEL", "gpt-4.1-mini")
        )
    return None


def analyze_documents(
    provider: AIResearchProvider | None, ticker: str, documents: list[dict[str, Any]]
) -> AIResearchResult | None:
    """Missing/failed optional AI remains missing and never crashes deterministic screening."""
    if provider is None or not documents:
        return None
    try:
        return provider.analyze(ticker, documents)
    except Exception:
        return None


def _reject_price_target(value: str) -> str:
    if re.search(r"\b(?:price\s+target|target\s+price)\b", value, re.IGNORECASE):
        raise ValueError("AI research must not contain a price target")
    return value
