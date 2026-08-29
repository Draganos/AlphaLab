"""Structured, evidence-bearing AI analyst layer with an offline deterministic provider."""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceReference(BaseModel):
    document_id: int
    excerpt: str


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
        if "price target" in value.lower():
            raise ValueError("AI research must not contain a price target")
        return value

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


def analyze_documents(
    provider: AIResearchProvider | None, ticker: str, documents: list[dict[str, Any]]
) -> AIResearchResult | None:
    """Missing/failed optional AI remains missing and never crashes deterministic screening."""
    if provider is None or not documents:
        return None
    try:
        return provider.analyze(ticker, documents)
    except (ValueError, TypeError, KeyError):
        return None
