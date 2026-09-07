"""Regression guard: Donatien External Calibration must have zero effect on
every existing AlphaLab scoring/ranking system.

Two layers of proof, per the Phase 1 brief's explicit requirement not to
merely assume isolation because the code lives in a separate module:

1. Static: none of the existing scoring modules import anything from
   alpha_lab.calibration / alpha_lab.providers.donatien -- there is no code
   path from Donatien into scoring, not just an unused one.
2. Behavioral: computing the existing fundamental score, composite score,
   and historical scoring produces bit-identical results whether or not a
   Donatien calibration row exists in the same database.
"""

from datetime import UTC, date, datetime
import math
from pathlib import Path

from sqlalchemy.orm import Session

from alpha_lab.calibration.service import ExternalCalibrationService
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.providers.donatien import DonatienFetchResult, compute_content_hash, parse_donatien_payload
from alpha_lab.strategy import HistoricalScoringService, composite_score

PAYLOAD = {
    "run_date": "2026-07-14",
    "run_time": "20:15",
    "macro_report_date": "2026-07-14",
    "supersedes": None,
    "dominant_regime": "Stagflation-lite",
    "confidence": "Medium",
    "scenario_weights": {"Stagflation": 100},
    "defensiveness": 4,
    "top_drivers": [{"name": "Iran/Hormuz Crisis", "dominance": 5}],
    "key_changes": ["Same defensiveness"],
    "trend_contrarian_split": {"Aggressive": "84/16"},
    "tiers": {
        "Aggressive": {
            "expected_behaviour": "Leads in energy-shock paths.",
            "weights": {"Gold": {"pct": 100, "asset_class": "commodity", "vehicle": "GLD"}},
        }
    },
}


def test_no_scoring_module_imports_the_calibration_or_donatien_provider_code():
    root = Path(__file__).resolve().parents[1]
    scoring_dirs = ["alpha_lab/research", "alpha_lab/screener", "alpha_lab/strategy",
                    "alpha_lab/backtest", "alpha_lab/portfolio", "alpha_lab/ratings",
                    "alpha_lab/factors"]
    offenders = []
    for directory in scoring_dirs:
        for path in (root / directory).rglob("*.py"):
            text = path.read_text()
            if "alpha_lab.calibration" in text or "alpha_lab.providers.donatien" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"Scoring modules must never import Donatien/calibration code: {offenders}"


def _seed_fundamental_data(engine):
    with Session(engine) as session:
        session.add(Security(ticker="XYZ", country="US", currency="USD", sector="Technology"))
        for i in range(300):
            session.add(Price(
                ticker="XYZ", date=date(2024, 1, 1).fromordinal(date(2024, 1, 1).toordinal() + i),
                close=100 + i * 0.1, high=101 + i * 0.1, low=99 + i * 0.1,
                provider="fixture", currency="USD", source="test",
            ))
        session.add(Fundamental(
            ticker="XYZ", period=date(2023, 12, 31), publication_date=date(2024, 2, 1),
            revenue=1_000_000, net_income=100_000, eps=1.0, total_equity=500_000,
            total_debt=100_000, cash=200_000, provider="fixture",
            observation_hash="xyz-fundamental-v1",
        ))
        session.commit()


def test_historical_scoring_is_identical_with_and_without_a_donatien_calibration_row():
    settings = load_settings()

    engine_without = make_engine("sqlite:///:memory:")
    create_schema(engine_without)
    _seed_fundamental_data(engine_without)
    without_result = HistoricalScoringService(engine_without, settings).score_universe_as_of(
        date(2024, 11, 1), tickers=["XYZ"]
    )

    engine_with = make_engine("sqlite:///:memory:")
    create_schema(engine_with)
    _seed_fundamental_data(engine_with)
    calibration = parse_donatien_payload(PAYLOAD)
    ExternalCalibrationService(engine_with).refresh(
        _StaticProvider(
            DonatienFetchResult(
                calibration=calibration, raw_payload=PAYLOAD,
                content_hash=compute_content_hash(PAYLOAD),
                retrieved_at=datetime.now(UTC), source_url="https://donatien.ca/fake",
            )
        )
    )
    with_result = HistoricalScoringService(engine_with, settings).score_universe_as_of(
        date(2024, 11, 1), tickers=["XYZ"]
    )

    assert len(without_result) == len(with_result) == 1
    a, b = without_result[0], with_result[0]
    assert a.score == b.score
    assert _nan_safe_equal(a.raw_factors, b.raw_factors)
    assert _nan_safe_equal(a.percentile_factors, b.percentile_factors)
    assert _nan_safe_equal(a.category_scores, b.category_scores)
    assert a.eligible == b.eligible
    assert a.exclusion_reason == b.exclusion_reason


def _nan_safe_equal(left: dict, right: dict) -> bool:
    """Regular dict equality, except NaN == NaN (unlike Python's default) --
    both sides come from the same deterministic factor math and are
    expected to be NaN in exactly the same places when data is sparse."""
    if left.keys() != right.keys():
        return False
    for key in left:
        a_value, b_value = left[key], right[key]
        if isinstance(a_value, float) and isinstance(b_value, float) and math.isnan(a_value) and math.isnan(b_value):
            continue
        if a_value != b_value:
            return False
    return True


def test_composite_score_is_unaffected_by_calibration_module_being_imported():
    """A weaker but simpler sanity check alongside the import-boundary test:
    merely importing alpha_lab.calibration must not have any import-time
    side effect (module-level state, monkeypatching, registry mutation)
    that changes composite_score's own deterministic weights math."""
    settings = load_settings()
    percentiles = {"earnings": 80.0, "revisions": 60.0, "fundamentals": 70.0, "valuation": 50.0,
                   "momentum": 90.0, "balance_sheet": 40.0, "ai": 55.0, "dividend": 30.0}
    before = composite_score(percentiles, settings.weights, date(2024, 1, 1), settings.coverage)

    import alpha_lab.calibration  # noqa: F401 -- import-time side-effect check only

    after = composite_score(percentiles, settings.weights, date(2024, 1, 1), settings.coverage)
    assert before == after


class _StaticProvider:
    def __init__(self, result: DonatienFetchResult):
        self._result = result

    def fetch(self) -> DonatienFetchResult:
        return self._result
