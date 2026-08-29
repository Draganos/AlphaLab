"""Deterministic, versioned Sharia-preferred business-activity screening."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
import hashlib
import json

from pydantic import BaseModel, Field
import yaml


class EthicalStatus(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    EXCLUDED = "EXCLUDED"
    UNKNOWN = "UNKNOWN"


class EthicsPolicy(BaseModel):
    mode: str = "sharia_preferred"
    policy_version: str
    hard_exclusions: dict[str, bool]
    review_categories: dict[str, bool]
    allowed_categories: dict[str, bool]
    financial_screen: dict[str, bool]
    unknown_company_policy: str = "review"
    manual_allow: list[str] = Field(default_factory=list)
    manual_exclude: list[str] = Field(default_factory=list)

    @property
    def deterministic_version(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return (
            f"{self.policy_version}:{hashlib.sha256(payload.encode()).hexdigest()[:12]}"
        )


class BusinessEvidence(BaseModel):
    ticker: str
    primary_business: str | None = None
    business_tags: list[str] = Field(default_factory=list)
    evidence: list[dict[str, str]] = Field(default_factory=list)
    source: str | None = None
    financial_warnings: list[str] = Field(default_factory=list)
    sector: str | None = None
    industry: str | None = None


class EthicalDecision(BaseModel):
    ticker: str
    ethical_status: EthicalStatus
    primary_business: str | None
    business_tags: list[str]
    exclusion_reasons: list[str]
    review_reasons: list[str]
    evidence: list[dict[str, str]]
    source: str | None
    evaluated_at: datetime
    policy_version: str
    manual_override: bool = False
    manual_override_reason: str | None = None
    financial_warnings: list[str] = Field(default_factory=list)
    evidence_fingerprint: str


def load_ethics_policy(path: str | Path = "config/ethics.yaml") -> EthicsPolicy:
    with Path(path).open(encoding="utf-8") as stream:
        return EthicsPolicy.model_validate(yaml.safe_load(stream))


def evaluate_business(
    evidence: BusinessEvidence,
    policy: EthicsPolicy,
    evaluated_at: datetime | None = None,
) -> EthicalDecision:
    """Apply business-activity rules; debt warnings never trigger hard exclusion."""
    ticker = evidence.ticker.upper()
    tags = {_canonical(tag) for tag in evidence.business_tags}
    tags.update(
        _infer_business_tags(" ".join(evidence.business_tags).replace("_", " "))
    )
    tags.update(
        _infer_business_tags(
            evidence.primary_business, evidence.sector, evidence.industry
        )
    )
    hard = sorted(
        rule
        for rule, enabled in policy.hard_exclusions.items()
        if enabled and rule in tags
    )
    reviews = sorted(
        rule
        for rule, enabled in policy.review_categories.items()
        if enabled and rule in tags
    )
    manual_override = False
    override_reason = None
    if ticker in {item.upper() for item in policy.manual_exclude}:
        status = EthicalStatus.EXCLUDED
        hard.append("manual_exclude")
        manual_override, override_reason = True, "Policy manual_exclude"
    elif hard:
        status = EthicalStatus.EXCLUDED
        if ticker in {item.upper() for item in policy.manual_allow}:
            reviews.append(
                "manual_allow ignored because a deterministic hard exclusion triggered"
            )
    elif ticker in {item.upper() for item in policy.manual_allow}:
        status = EthicalStatus.PASS
        manual_override, override_reason = True, "Policy manual_allow"
    elif reviews:
        status = EthicalStatus.REVIEW
    elif tags & {
        rule for rule, enabled in policy.allowed_categories.items() if enabled
    }:
        status = EthicalStatus.PASS
    elif "general_operating_business" in tags:
        status = EthicalStatus.PASS
    else:
        status = (
            EthicalStatus.REVIEW
            if policy.unknown_company_policy == "review"
            else EthicalStatus.UNKNOWN
        )
        reviews.append("insufficient business evidence")
    return EthicalDecision(
        ticker=ticker,
        ethical_status=status,
        primary_business=evidence.primary_business,
        business_tags=sorted(tags),
        exclusion_reasons=hard,
        review_reasons=reviews,
        evidence=evidence.evidence,
        source=evidence.source,
        evaluated_at=evaluated_at or datetime.now(UTC),
        policy_version=policy.deterministic_version,
        manual_override=manual_override,
        manual_override_reason=override_reason,
        financial_warnings=evidence.financial_warnings,
        evidence_fingerprint=evidence_fingerprint(evidence),
    )


def _canonical(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def evidence_fingerprint(evidence: BusinessEvidence) -> str:
    """Hash only attributable classification inputs, not evaluation time."""
    payload = json.dumps(
        evidence.model_dump(), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _infer_business_tags(
    primary_business: str | None, sector: str | None = None, industry: str | None = None
) -> set[str]:
    """Conservatively classify explicit business descriptions, never ticker names."""
    text = " ".join(
        value or "" for value in (primary_business, sector, industry)
    ).casefold()
    if not text.strip():
        return set()
    rules = {
        "conventional_banking": (
            "conventional bank",
            "commercial bank",
            "investment bank",
            "bank holding company",
            "banking services",
            "accepts deposits",
            "deposit-taking",
            "deposit taking",
            "commercial lending",
            "deposits",
            "banking products",
        ),
        "interest_based_lending": (
            "interest-based lending",
            "interest based lending",
            "mortgage lender",
            "payday lender",
            "consumer finance",
            "consumer lending",
            "mortgage finance",
            "credit lending",
            "specialty finance",
            "loan origination",
            "provides loans",
            "loans",
        ),
        "weapons": (
            "weapons manufacturing",
            "firearms manufacturing",
            "missile systems",
            "ammunition",
            "arms manufacturer",
            "weapon systems",
            "missile system",
        ),
        "gambling": ("casino operator", "gambling operator", "sports betting"),
        "alcohol_production": ("alcohol producer", "brewery", "distillery"),
        "tobacco": ("tobacco producer", "nicotine products"),
        "adult_entertainment": ("adult entertainment",),
        "pork_primary_business": ("pork producer", "pork processing"),
        "payment_processing": ("payment processing", "payment processor"),
        "payment_networks": ("payment network",),
        "airlines": ("passenger aviation", "airline operator"),
        "mixed_financial_services": ("financial services", "financial conglomerate"),
        "conventional_insurance": (
            "insurance carrier",
            "life insurance",
            "property insurance",
        ),
        "defence_non_weapon_supplier": (
            "aerospace & defense",
            "defence contractor",
            "defense contractor",
        ),
    }
    tags = {
        tag
        for tag, phrases in rules.items()
        if any(phrase in text for phrase in phrases)
    }
    if industry and "bank" in industry.casefold():
        tags.add("conventional_banking")
    payment = bool(tags & {"payment_processing", "payment_networks"})
    hard = bool(tags & {"conventional_banking", "interest_based_lending"})
    if payment and not hard:
        tags.discard("mixed_financial_services")
    financial = "financial" in text or "bank" in text or "finance" in text
    if financial and not payment and not hard:
        tags.add("mixed_financial_services")
    clearly_described = bool(
        primary_business and len(primary_business.strip()) >= 24 and industry
    )
    if (
        clearly_described
        and not financial
        and not (tags & {"defence_non_weapon_supplier"})
    ):
        tags.add("general_operating_business")
    return tags
