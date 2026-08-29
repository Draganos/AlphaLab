import pytest
from alpha_lab.ethics import (
    BusinessEvidence,
    EthicalStatus,
    evaluate_business,
    load_ethics_policy,
)
from alpha_lab.portfolio import Candidate, construct_portfolio


def test_required_business_rules_and_policy_version():
    policy = load_ethics_policy()
    cases = {
        "BANK": (["conventional_banking"], EthicalStatus.EXCLUDED),
        "ARMS": (["weapons"], EthicalStatus.EXCLUDED),
        "AIR": (["airlines"], EthicalStatus.PASS),
        "PAY": (["payment_processing"], EthicalStatus.PASS),
        "UNK": ([], EthicalStatus.REVIEW),
    }
    for ticker, (tags, expected) in cases.items():
        result = evaluate_business(
            BusinessEvidence(
                ticker=ticker,
                primary_business="Known" if tags else None,
                business_tags=tags,
                financial_warnings=["high debt"],
            ),
            policy,
        )
        assert result.ethical_status == expected
        assert result.policy_version.startswith("sharia-preferred-v1:")
    assert (
        evaluate_business(
            BusinessEvidence(
                ticker="AIR",
                primary_business="Aviation",
                business_tags=["airlines"],
                financial_warnings=["high debt"],
            ),
            policy,
        ).ethical_status
        == EthicalStatus.PASS
    )


def test_hard_exclusion_beats_manual_allow_and_score():
    policy = load_ethics_policy().model_copy(update={"manual_allow": ["BANK"]})
    decision = evaluate_business(
        BusinessEvidence(
            ticker="BANK",
            primary_business="Bank",
            business_tags=["conventional_banking"],
        ),
        policy,
    )
    assert decision.ethical_status == EthicalStatus.EXCLUDED
    result = construct_portfolio(
        [Candidate("BANK", 99, 1, ethical_status="EXCLUDED")],
        method="equal",
        min_score=0,
        minimum_coverage=0,
        min_positions=1,
        max_positions=1,
        max_position=1,
        max_sector=None,
        ethical_filter_enabled=True,
    )
    assert result.weights == {}
    assert result.excluded["BANK"] == "ethical status excluded"


def test_manual_exclude():
    policy = load_ethics_policy().model_copy(update={"manual_exclude": ["AIR"]})
    result = evaluate_business(
        BusinessEvidence(
            ticker="AIR", primary_business="Aviation", business_tags=["airlines"]
        ),
        policy,
    )
    assert result.ethical_status == EthicalStatus.EXCLUDED
    assert result.manual_override


def test_explicit_primary_business_cannot_silently_pass_without_tags():
    policy = load_ethics_policy()
    bank = evaluate_business(
        BusinessEvidence(ticker="B", primary_business="Conventional commercial bank"),
        policy,
    )
    weapons = evaluate_business(
        BusinessEvidence(ticker="W", primary_business="Weapons manufacturing"), policy
    )
    assert bank.ethical_status == EthicalStatus.EXCLUDED
    assert weapons.ethical_status == EthicalStatus.EXCLUDED


def test_generic_bank_and_lending_descriptions_are_never_silent_passes():
    policy = load_ethics_policy()
    descriptions = [
        "A bank holding company offering deposits and loans",
        "Provides banking services and commercial lending",
        "Consumer finance and mortgage finance company",
        "Specialty finance business providing credit lending",
        "Investment banking and loan origination services",
    ]
    for index, description in enumerate(descriptions):
        result = evaluate_business(
            BusinessEvidence(
                ticker=f"B{index}",
                primary_business=description,
                sector="Financials",
                industry="Banks",
            ),
            policy,
        )
        assert result.ethical_status == EthicalStatus.EXCLUDED


def test_ambiguous_financials_review_but_payment_infrastructure_passes():
    policy = load_ethics_policy()
    ambiguous = evaluate_business(
        BusinessEvidence(
            ticker="MIX",
            primary_business="Diversified financial services",
            sector="Financials",
            industry="Financial Services",
        ),
        policy,
    )
    payment = evaluate_business(
        BusinessEvidence(
            ticker="PAY",
            primary_business="Global payment network infrastructure",
            sector="Financials",
            industry="Payment Processing",
        ),
        policy,
    )
    assert ambiguous.ethical_status == EthicalStatus.REVIEW
    assert payment.ethical_status == EthicalStatus.PASS


def test_bank_tags_and_generic_bank_industry_are_excluded():
    policy = load_ethics_policy()
    tagged = evaluate_business(
        BusinessEvidence(ticker="TAG", business_tags=["bank_holding_company"]), policy
    )
    generic = evaluate_business(
        BusinessEvidence(
            ticker="IND",
            primary_business="Provides a broad range of services to customers",
            sector="Financials",
            industry="Regional Banks",
        ),
        policy,
    )
    assert tagged.ethical_status == EthicalStatus.EXCLUDED
    assert generic.ethical_status == EthicalStatus.EXCLUDED


@pytest.mark.parametrize(
    ("ticker", "industry", "description", "expected"),
    [
        (
            "TOB",
            "Tobacco",
            "Manufactures and sells cigarettes and cigars",
            EthicalStatus.EXCLUDED,
        ),
        (
            "CAS",
            "Gambling",
            "Operates casino resorts and sportsbooks",
            EthicalStatus.EXCLUDED,
        ),
        (
            "ALC",
            "Beverages - Wineries & Distilleries",
            "Produces and distributes spirits",
            EthicalStatus.EXCLUDED,
        ),
        (
            "ARM",
            "Aerospace & Defense",
            "Manufactures missile systems, weapons and munitions",
            EthicalStatus.EXCLUDED,
        ),
        (
            "AERO",
            "Aerospace & Defense",
            "Supplies non-weapon aerospace components for commercial aircraft",
            EthicalStatus.REVIEW,
        ),
        (
            "PAY2",
            "Payment Processing",
            "Operates payment processing and network infrastructure",
            EthicalStatus.PASS,
        ),
        (
            "LEND",
            "Payment Processing",
            "Operates payments and consumer lending as its primary business",
            EthicalStatus.EXCLUDED,
        ),
        (
            "AIR2",
            "Airlines",
            "Provides scheduled passenger air transportation and serves alcoholic beverages onboard",
            EthicalStatus.PASS,
        ),
        (
            "ADULT",
            "Adult Entertainment",
            "Operates adult entertainment venues",
            EthicalStatus.EXCLUDED,
        ),
        (
            "PORK",
            "Pork Processing",
            "Pork processing is the primary business",
            EthicalStatus.EXCLUDED,
        ),
    ],
)
def test_industry_aware_activity_classification(
    ticker, industry, description, expected
):
    result = evaluate_business(
        BusinessEvidence(
            ticker=ticker,
            primary_business=description,
            sector="Financials" if "Payment" in industry else "Industrials",
            industry=industry,
        ),
        load_ethics_policy(),
    )
    assert result.ethical_status == expected
