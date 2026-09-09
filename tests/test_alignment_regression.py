"""Regression guard: Phase 2B Alignment must have zero effect on every
existing AlphaLab scoring/ranking system, and must not be reachable from
alpha_lab.macro/alpha_lab.calibration/alpha_lab.providers.donatien --
mirroring tests/test_macro_regression.py and
tests/test_calibration_regression.py's two-layer proof, extended with the
reverse-direction check the Phase 2B brief explicitly requires (the
direction must strictly be Market Regime + Donatien -> Alignment ->
Dashboard, never the other way)."""

from datetime import UTC, date, datetime
import math
from pathlib import Path

from sqlalchemy.orm import Session

from alpha_lab.alignment.service import AlignmentService
from alpha_lab.calibration.service import ExternalCalibrationService
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.providers.donatien import DonatienFetchResult, compute_content_hash, parse_donatien_payload
from alpha_lab.strategy import HistoricalScoringService

_PAYLOAD = {
    "run_date": "2026-07-14",
    "run_time": "20:15",
    "macro_report_date": "2026-07-14",
    "supersedes": None,
    "dominant_regime": "Stagflation-lite",
    "confidence": "Medium",
    "scenario_weights": {"Stagflation": 60, "Soft Landing": 20, "Reacceleration": 10, "Deflationary Bust": 10},
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


class _StaticDonatienProvider:
    def __init__(self, result: DonatienFetchResult):
        self._result = result

    def fetch(self) -> DonatienFetchResult:
        return self._result


def test_no_scoring_module_imports_the_alignment_code():
    root = Path(__file__).resolve().parents[1]
    scoring_dirs = ["alpha_lab/research", "alpha_lab/screener", "alpha_lab/strategy",
                    "alpha_lab/backtest", "alpha_lab/portfolio", "alpha_lab/ratings",
                    "alpha_lab/factors"]
    offenders = []
    for directory in scoring_dirs:
        for path in (root / directory).rglob("*.py"):
            text = path.read_text()
            if "alpha_lab.alignment" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"Scoring modules must never import alignment code: {offenders}"


def test_macro_and_calibration_never_import_the_alignment_code():
    """Reverse-direction boundary: the dependency must flow Market Regime +
    Donatien -> Alignment, never Alignment -> Market Regime/Donatien's own
    modules importing back into it (which would indicate a circular or
    inverted design)."""
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for directory in ["alpha_lab/macro", "alpha_lab/calibration", "alpha_lab/providers"]:
        for path in (root / directory).rglob("*.py"):
            text = path.read_text()
            if "alpha_lab.alignment" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"Market Regime/Donatien must never import alignment code: {offenders}"


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
            observation_hash="xyz-fundamental-alignment-v1",
        ))
        session.commit()


def test_historical_scoring_is_identical_with_and_without_an_alignment_assessment():
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
    calibration = parse_donatien_payload(_PAYLOAD)
    ExternalCalibrationService(engine_with).refresh(
        _StaticDonatienProvider(DonatienFetchResult(
            calibration=calibration, raw_payload=_PAYLOAD,
            content_hash=compute_content_hash(_PAYLOAD),
            retrieved_at=datetime.now(UTC), source_url="https://donatien.ca/fake",
        ))
    )
    # No macro assessment exists in engine_with -> alignment degrades to
    # INSUFFICIENT_DATA; the point is that computing and persisting an
    # alignment row at all must not perturb scoring.
    AlignmentService(engine_with).refresh(as_of=date(2024, 11, 1))

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
    if left.keys() != right.keys():
        return False
    for key in left:
        a_value, b_value = left[key], right[key]
        if isinstance(a_value, float) and isinstance(b_value, float) and math.isnan(a_value) and math.isnan(b_value):
            continue
        if a_value != b_value:
            return False
    return True


def test_alignment_refresh_writes_no_price_or_fundamental_rows():
    """AlignmentService makes no network call and reads only already-stored
    evidence -- confirms it never writes to Price/Fundamental, the tables
    scoring actually reads."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    _seed_fundamental_data(engine)
    with Session(engine) as session:
        price_count_before = session.query(Price).count()
        fundamental_count_before = session.query(Fundamental).count()

    AlignmentService(engine).refresh(as_of=date(2024, 11, 1))

    with Session(engine) as session:
        assert session.query(Price).count() == price_count_before
        assert session.query(Fundamental).count() == fundamental_count_before
