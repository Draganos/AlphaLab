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
