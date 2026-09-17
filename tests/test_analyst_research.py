"""Deterministic tests for alpha_lab.research.analyst_research's pure
build_analyst_research_summary function. No network, no database --
fake objects stand in for AnalystRatingChange/EstimateRevisionTrend ORM
rows."""

from datetime import date, datetime

from alpha_lab.research.analyst_research import (
    RevisionDirection,
    build_analyst_research_summary,
)


class _FakeChange:
    def __init__(self, id, grade_date, action):
        self.id = id
        self.grade_date = grade_date
        self.action = action
        self.firm = "Test Firm"
        self.to_grade = "Buy"
        self.from_grade = "Hold"
        self.price_target_action = "Raises"
        self.current_price_target = 100.0
        self.prior_price_target = 90.0


class _FakeTrend:
    def __init__(self, id, fiscal_period, current, ago7=None, ago30=None, ago60=None, ago90=None):
        self.id = id
        self.fiscal_period = fiscal_period
        self.eps_trend_current = current
        self.eps_trend_7d_ago = ago7
        self.eps_trend_30d_ago = ago30
        self.eps_trend_60d_ago = ago60
        self.eps_trend_90d_ago = ago90
        self.revisions_up_last_7d = 2
        self.revisions_up_last_30d = 5
        self.revisions_down_last_7d = 0
        self.revisions_down_last_30d = 1
        self.observation_date = date(2026, 9, 17)


def test_returns_none_when_neither_domain_has_any_data():
    """Matches analyst_consensus/technical_summary's "None means not
    computed" convention -- never an empty-but-present object."""
    assert build_analyst_research_summary("NVDA", [], [], as_of=date(2026, 9, 17)) is None


def test_coverage_is_domain_aware_when_only_one_domain_present():
    only_changes = build_analyst_research_summary(
        "NVDA", [_FakeChange(1, datetime(2026, 9, 1), "up")], [], as_of=date(2026, 9, 17)
    )
    assert only_changes.coverage == 0.5
    only_trend = build_analyst_research_summary(
        "NVDA", [], [_FakeTrend(1, date(2027, 1, 25), 10.0, ago30=9.0)], as_of=date(2026, 9, 17)
    )
    assert only_trend.coverage == 0.5


def test_coverage_is_full_when_both_domains_present():
    summary = build_analyst_research_summary(
        "NVDA",
        [_FakeChange(1, datetime(2026, 9, 1), "up")],
        [_FakeTrend(1, date(2027, 1, 25), 10.0, ago30=9.0)],
        as_of=date(2026, 9, 17),
    )
    assert summary.coverage == 1.0


def test_rating_change_counts_90d_is_none_not_zero_when_no_history_at_all():
    """Genuinely no rating-change history at all -- counts must be None,
    never a fabricated {"upgrades": 0, ...}."""
    summary = build_analyst_research_summary(
        "NVDA", [], [_FakeTrend(1, date(2027, 1, 25), 10.0, ago30=9.0)], as_of=date(2026, 9, 17)
    )
    assert summary.rating_change_counts_90d is None


def test_rating_change_counts_90d_is_a_genuine_zero_when_history_exists_but_window_is_empty():
    """History exists but nothing happened in the trailing 90 days --
    a real, non-fabricated zero."""
    old_change = _FakeChange(1, datetime(2025, 1, 1), "up")  # far outside the window
    summary = build_analyst_research_summary(
        "NVDA", [old_change], [], as_of=date(2026, 9, 17)
    )
    assert summary.rating_change_counts_90d == {
        "upgrades": 0, "downgrades": 0, "initiations": 0, "reiterations": 0,
    }


def test_rating_change_counts_90d_tallies_by_action_within_the_window():
    changes = [
        _FakeChange(1, datetime(2026, 9, 10), "up"),
        _FakeChange(2, datetime(2026, 9, 1), "up"),
        _FakeChange(3, datetime(2026, 8, 1), "down"),
        _FakeChange(4, datetime(2026, 7, 1), "init"),
        _FakeChange(5, datetime(2026, 7, 15), "main"),
        _FakeChange(6, datetime(2020, 1, 1), "up"),  # far outside 90d window
    ]
    summary = build_analyst_research_summary("NVDA", changes, [], as_of=date(2026, 9, 17))
    assert summary.rating_change_counts_90d == {
        "upgrades": 2, "downgrades": 1, "initiations": 1, "reiterations": 1,
    }


def test_recent_rating_changes_is_bounded_by_limit():
    changes = [_FakeChange(i, datetime(2026, 9, 1), "up") for i in range(15)]
    summary = build_analyst_research_summary(
        "NVDA", changes, [], as_of=date(2026, 9, 17), recent_changes_limit=10
    )
    assert len(summary.recent_rating_changes) == 10


def test_revision_direction_improving_when_current_above_30d_ago():
    summary = build_analyst_research_summary(
        "NVDA", [], [_FakeTrend(1, date(2027, 1, 25), 10.0, ago30=9.0)], as_of=date(2026, 9, 17)
    )
    assert summary.revision_trend[0].direction == RevisionDirection.IMPROVING


def test_revision_direction_deteriorating_when_current_below_30d_ago():
    summary = build_analyst_research_summary(
        "NVDA", [], [_FakeTrend(1, date(2027, 1, 25), 8.0, ago30=9.0)], as_of=date(2026, 9, 17)
    )
    assert summary.revision_trend[0].direction == RevisionDirection.DETERIORATING


def test_revision_direction_stable_when_current_equals_30d_ago():
    summary = build_analyst_research_summary(
        "NVDA", [], [_FakeTrend(1, date(2027, 1, 25), 9.0, ago30=9.0)], as_of=date(2026, 9, 17)
    )
    assert summary.revision_trend[0].direction == RevisionDirection.STABLE


def test_revision_direction_is_review_never_guessed_when_data_missing():
    summary = build_analyst_research_summary(
        "NVDA", [], [_FakeTrend(1, date(2027, 1, 25), None, ago30=9.0)], as_of=date(2026, 9, 17)
    )
    assert summary.revision_trend[0].direction == RevisionDirection.REVIEW
    summary_missing_prior = build_analyst_research_summary(
        "NVDA", [], [_FakeTrend(1, date(2027, 1, 25), 9.0, ago30=None)], as_of=date(2026, 9, 17)
    )
    assert summary_missing_prior.revision_trend[0].direction == RevisionDirection.REVIEW


def test_evidence_ids_trace_back_to_underlying_rows():
    summary = build_analyst_research_summary(
        "NVDA",
        [_FakeChange(42, datetime(2026, 9, 1), "up")],
        [_FakeTrend(7, date(2027, 1, 25), 10.0, ago30=9.0)],
        as_of=date(2026, 9, 17),
    )
    assert "analyst_rating_change:42" in summary.evidence_ids
    assert "estimate_revision_trend:7" in summary.evidence_ids
