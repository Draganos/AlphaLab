"""Deterministic, versioned Sharia-preferred business-activity screening."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
import hashlib
import json
import re

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
    elif (
        "general_operating_business" in tags
        and policy.allowed_categories.get("general_operating_business", False)
    ):
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
    description = (primary_business or "").casefold()
    industry_text = (industry or "").casefold()
    sector_text = (sector or "").casefold()
    text = " ".join((description, sector_text, industry_text)).strip()
    if not text:
        return set()
    tags: set[str] = set()
    payment = _matches_any(
        text,
        (
            "payment processing",
            "payment processor",
            "payment network",
            "merchant acquiring",
        ),
    )
    lending = _matches_any(
        description,
        (
            "lending",
            "lender",
            "loans",
            "consumer credit",
            "consumer finance",
            "mortgage",
            "credit finance",
            "specialty finance",
            "loan origination",
            "commercial credit",
        ),
    ) or _matches_any(
        industry_text,
        ("lending", "consumer finance", "mortgage finance", "specialty finance"),
    )
    banking_industry = _matches_any(industry_text, ("bank", "banks", "banking"))
    banking_description = _matches_any(
        description,
        (
            "bank holding company",
            "commercial bank",
            "investment bank",
            "banking services",
            "accepts deposits",
            "deposit taking",
            "deposit-taking",
            "banking products",
            "operates a bank",
            "is a bank",
            "national bank",
            "community bank",
            "retail bank",
        ),
    ) or bool(re.search(r"^(?:a |an |the )?banks?\b", description))
    if banking_industry or banking_description:
        tags.add("conventional_banking")
    if lending:
        tags.add("interest_based_lending")
    if payment:
        tags.add(
            "payment_processing"
            if "processing" in text or "processor" in text
            else "payment_networks"
        )

    weapons_terms = (
        "weapon",
        "weapons",
        "firearm",
        "firearms",
        "ammunition",
        "missile",
        "missiles",
        "munitions",
        "combat weapons",
        "military weapons systems",
        "ordnance",
    )
    weapons_description = re.sub(
        r"\b(?:non[- ]weapon|without weapons?)\b", "", description
    )
    defence_industry = _matches_any(
        industry_text, ("aerospace & defense", "aerospace and defense", "defence")
    )
    arms_activity = _matches_any(
        description, ("arms manufacturer", "arms manufacturing", "produces arms")
    ) or (defence_industry and _matches_any(description, ("arms",)))
    if (
        _matches_any(weapons_description, weapons_terms)
        or arms_activity
        or _matches_any(
            industry_text,
            ("weapons", "firearms", "ammunition", "munitions", "ordnance"),
        )
    ):
        tags.add("weapons")

    if _matches_any(
        industry_text, ("tobacco", "cigarettes", "nicotine")
    ) or _matches_any(
        description,
        (
            "tobacco",
            "cigarette",
            "cigarettes",
            "cigar",
            "cigars",
            "nicotine",
            "vaping",
            "vape",
        ),
    ):
        tags.add("tobacco")
    gambling_industry = _matches_any(
        industry_text, ("gambling", "casino", "sportsbook", "betting", "wagering")
    )
    gambling_description = _matches_any(
        description,
        ("gambling", "casino", "casinos", "sportsbook", "betting", "wagering"),
    )
    gaming_operator = "gaming operator" in description and gambling_industry
    if gambling_industry or gambling_description or gaming_operator:
        tags.add("gambling")
    alcohol_industry = _matches_any(
        industry_text, ("winery", "wineries", "distillery", "distilleries", "breweries")
    )
    alcohol_description = _matches_any(
        description,
        (
            "brewery",
            "breweries",
            "brewer",
            "distillery",
            "distilleries",
            "spirits producer",
            "wine producer",
            "winery",
        ),
    ) or (
        "alcoholic beverages" in description
        and _matches_any(description, ("produces", "manufactures", "brews", "distills"))
    )
    if alcohol_industry or alcohol_description:
        tags.add("alcohol_production")
    if _matches_any(
        industry_text, ("adult entertainment", "pornography")
    ) or _matches_any(
        description,
        ("adult entertainment", "pornographic", "pornography", "sexually explicit"),
    ):
        tags.add("adult_entertainment")
    if _matches_any(industry_text, ("pork", "hog farming", "swine")) or _matches_any(
        description,
        (
            "pork producer",
            "pork processing",
            "hog farming",
            "raises hogs",
            "swine production",
        ),
    ):
        tags.add("pork_primary_business")

    airline = _matches_any(
        text,
        ("passenger aviation", "airline operator", "airlines", "air transportation"),
    )
    if airline:
        tags.add("airlines")
    defence = defence_industry or _matches_any(
        description, ("defence contractor", "defense contractor", "military supplier")
    )
    if defence and "weapons" not in tags:
        tags.add("defence_non_weapon_supplier")
    if _matches_any(
        text, ("insurance carrier", "life insurance", "property insurance")
    ):
        tags.add("conventional_insurance")

    hard_finance = bool(tags & {"conventional_banking", "interest_based_lending"})
    financial = sector_text == "financials" or _matches_any(
        industry_text, ("financial services", "finance")
    )
    if financial and not payment and not hard_finance:
        tags.add("mixed_financial_services")
    clearly_described = bool(
        primary_business and len(primary_business.strip()) >= 24 and industry
    )
    if (
        clearly_described
        and not financial
        and not (tags & {"defence_non_weapon_supplier"})
        and not _matches_any(
            industry_text, ("gaming", "beverages", "conglomerate", "miscellaneous")
        )
    ):
        tags.add("general_operating_business")
    return tags


def _matches_any(text: str, terms: tuple[str, ...]) -> bool:
    """Match complete words/phrases so e.g. 'arms' does not match 'pharmaceuticals'."""
    return any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) for term in terms)
