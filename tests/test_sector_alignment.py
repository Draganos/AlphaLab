"""Offline, deterministic tests for CalibrationAlignment (security-level):
the Morningstar-to-GICS sector bridge and the Donatien tier-weight lookup
it enables. No network access."""

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from alpha_lab.calibration.sector_alignment import (
    SectorTierWeight,
    get_sector_tier_weights_for_ticker,
    sector_tier_weights,
)
from alpha_lab.calibration.sector_taxonomy import (
    MORNINGSTAR_TO_GICS_SECTOR,
    approximate_gics_sector,
)
from alpha_lab.calibration.service import ExternalCalibrationService
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Security
from alpha_lab.providers.donatien import (
    DonatienFetchResult,
    compute_content_hash,
    parse_donatien_payload,
)

_PAYLOAD = {
    "run_date": "2026-07-14", "run_time": "20:15", "macro_report_date": "2026-07-14",
    "supersedes": None, "dominant_regime": "Stagflation-lite", "confidence": "Medium",
    "scenario_weights": {"Stagflation": 60, "Soft Landing": 20, "Reacceleration": 10, "Deflationary Bust": 10},
    "defensiveness": 4, "top_drivers": [], "key_changes": [], "trend_contrarian_split": {},
    "tiers": {
        "Aggressive": {
            "expected_behaviour": "x",
            "weights": {
                "Energy": {"pct": 11, "asset_class": "equity", "vehicle": "XLE", "gics_sector": "Energy"},
                "Gold": {"pct": 11, "asset_class": "commodity", "vehicle": "GLD"},
            },
        },
        "Balanced": {
            "expected_behaviour": "x",
            "weights": {
                "Energy2": {"pct": 5, "asset_class": "equity", "vehicle": "XLE", "gics_sector": "Energy"},
                "Tech": {"pct": 8, "asset_class": "equity", "vehicle": "XLK", "gics_sector": "Information Technology"},
            },
        },
    },
}


class _StaticProvider:
    def __init__(self, calibration, raw_payload):
        self._calibration = calibration
        self._raw_payload = raw_payload

    def fetch(self) -> DonatienFetchResult:
        return DonatienFetchResult(
            calibration=self._calibration, raw_payload=self._raw_payload,
            content_hash=compute_content_hash(self._raw_payload),
            retrieved_at=datetime.now(UTC), source_url="https://donatien.example",
        )


# --- approximate_gics_sector --------------------------------------------


def test_all_eleven_morningstar_sectors_map_to_a_gics_sector():
    assert len(MORNINGSTAR_TO_GICS_SECTOR) == 11
    for morningstar_sector, gics_sector in MORNINGSTAR_TO_GICS_SECTOR.items():
        assert approximate_gics_sector(morningstar_sector) == gics_sector


def test_unrecognized_sector_string_returns_none_not_a_guess():
    assert approximate_gics_sector("Not A Real Sector") is None


def test_none_sector_returns_none():
    assert approximate_gics_sector(None) is None


# --- sector_tier_weights (pure) ------------------------------------------


def test_sector_tier_weights_finds_matches_across_multiple_tiers():
    calibration = parse_donatien_payload(_PAYLOAD)
    results = sector_tier_weights(calibration, morningstar_sector="Energy")
    assert len(results) == 2
    tiers = {row.tier for row in results}
    assert tiers == {"Aggressive", "Balanced"}
    for row in results:
        assert row.gics_sector == "Energy"
        assert row.vehicle == "XLE"


def test_sector_tier_weights_reports_donatiens_own_published_pct_verbatim():
    calibration = parse_donatien_payload(_PAYLOAD)
    results = sector_tier_weights(calibration, morningstar_sector="Energy")
    by_tier = {row.tier: row.pct for row in results}
    assert by_tier == {"Aggressive": 11.0, "Balanced": 5.0}


def test_sector_tier_weights_is_empty_for_an_unmapped_sector_string():
    calibration = parse_donatien_payload(_PAYLOAD)
    assert sector_tier_weights(calibration, morningstar_sector="Not A Real Sector") == []


def test_sector_tier_weights_is_empty_for_none_sector():
    calibration = parse_donatien_payload(_PAYLOAD)
    assert sector_tier_weights(calibration, morningstar_sector=None) == []


def test_sector_tier_weights_is_empty_when_no_tier_references_the_sector():
    calibration = parse_donatien_payload(_PAYLOAD)
    # "Real Estate" is a valid GICS sector but nothing in the fixture uses it.
    assert sector_tier_weights(calibration, morningstar_sector="Real Estate") == []


def test_sector_tier_weights_ignores_commodity_lines_with_no_gics_sector():
    """A tier weight line with no gics_sector at all (commodity/fixed_income/
    cash, per the Donatien schema) must never spuriously match any query."""
    calibration = parse_donatien_payload(_PAYLOAD)
    for sector in MORNINGSTAR_TO_GICS_SECTOR:
        for row in sector_tier_weights(calibration, morningstar_sector=sector):
            assert row.line_name != "Gold"


def test_sector_tier_weights_is_deterministic():
    calibration = parse_donatien_payload(_PAYLOAD)
    first = sector_tier_weights(calibration, morningstar_sector="Energy")
    second = sector_tier_weights(calibration, morningstar_sector="Energy")
    assert first == second


def test_no_alignment_verdict_or_score_field_on_sector_tier_weight():
    """This module deliberately produces no ALIGNED/CONFLICT/NEUTRAL verdict
    and no computed score -- only Donatien's own published pct, passed
    through verbatim. Assert the model has no such field."""
    forbidden = {"alignment", "score", "verdict", "confidence"}
    assert forbidden.isdisjoint(SectorTierWeight.model_fields.keys())


# --- get_sector_tier_weights_for_ticker (DB-backed) ----------------------


def _engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    return engine


def test_get_sector_tier_weights_for_ticker_happy_path():
    engine = _engine()
    with Session(engine) as session:
        session.add(Security(ticker="XOM", country="US", currency="USD", sector="Energy"))
        session.commit()
    calibration = parse_donatien_payload(_PAYLOAD)
    ExternalCalibrationService(engine).refresh(_StaticProvider(calibration, _PAYLOAD))

    results = get_sector_tier_weights_for_ticker(engine, "XOM")
    assert len(results) == 2
    assert {row.tier for row in results} == {"Aggressive", "Balanced"}


def test_get_sector_tier_weights_for_ticker_unknown_ticker_is_empty():
    engine = _engine()
    calibration = parse_donatien_payload(_PAYLOAD)
    ExternalCalibrationService(engine).refresh(_StaticProvider(calibration, _PAYLOAD))
    assert get_sector_tier_weights_for_ticker(engine, "NOT_A_TICKER") == []


def test_get_sector_tier_weights_for_ticker_no_stored_sector_is_empty():
    engine = _engine()
    with Session(engine) as session:
        session.add(Security(ticker="XOM", country="US", currency="USD", sector=None))
        session.commit()
    calibration = parse_donatien_payload(_PAYLOAD)
    ExternalCalibrationService(engine).refresh(_StaticProvider(calibration, _PAYLOAD))
    assert get_sector_tier_weights_for_ticker(engine, "XOM") == []


def test_get_sector_tier_weights_for_ticker_no_calibration_yet_is_empty():
    engine = _engine()
    with Session(engine) as session:
        session.add(Security(ticker="XOM", country="US", currency="USD", sector="Energy"))
        session.commit()
    # No ExternalCalibrationService.refresh() call at all.
    assert get_sector_tier_weights_for_ticker(engine, "XOM") == []


# --- regression: zero effect on scoring, zero new persistence -----------


def test_no_scoring_module_imports_sector_alignment_code():
    root = Path(__file__).resolve().parents[1]
    scoring_dirs = ["alpha_lab/research", "alpha_lab/screener", "alpha_lab/strategy",
                    "alpha_lab/backtest", "alpha_lab/portfolio", "alpha_lab/ratings",
                    "alpha_lab/factors"]
    offenders = []
    for directory in scoring_dirs:
        for path in (root / directory).rglob("*.py"):
            text = path.read_text()
            if "sector_alignment" in text or "sector_taxonomy" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"Scoring modules must never import CalibrationAlignment code: {offenders}"


def test_get_sector_tier_weights_writes_nothing_to_the_database():
    """Purely a read -- no network, no persistence. Confirms calling it
    repeatedly never grows any table."""
    from sqlalchemy import inspect

    engine = _engine()
    with Session(engine) as session:
        session.add(Security(ticker="XOM", country="US", currency="USD", sector="Energy"))
        session.commit()
    calibration = parse_donatien_payload(_PAYLOAD)
    ExternalCalibrationService(engine).refresh(_StaticProvider(calibration, _PAYLOAD))

    table_names = inspect(engine).get_table_names()
    before = {name: session_count(engine, name) for name in table_names}
    get_sector_tier_weights_for_ticker(engine, "XOM")
    get_sector_tier_weights_for_ticker(engine, "XOM")
    after = {name: session_count(engine, name) for name in table_names}
    assert before == after


def session_count(engine, table_name: str) -> int:
    from sqlalchemy import text

    with Session(engine) as session:
        return session.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
