"""Regression test for a real UX gap found while investigating why the
Alignment page can look "wrong" after refreshing Macro Regime or External
Calibration on their own pages: AlignmentService.refresh() only recomputes
from whatever is currently stored, and is never called automatically by
either of those two refreshes (by design -- see this codebase's own
"explicit refresh only" architecture). That leaves the Alignment page
displaying a comparison computed from now-superseded evidence, with no
visible indication at the top of the page that it's stale (only a caption
near the unrelated "Recompute alignment" button, easy to miss).

This test targets the page's own `_staleness_reasons` helper -- a pure,
read-only comparison between the stored AlignmentAssessment's own recorded
input timestamps and whatever Macro Regime / Donatien Calibration is
currently stored. It never recomputes or writes anything; it only decides
whether to show a warning."""

from datetime import date, datetime
import importlib.util
from pathlib import Path

from sqlalchemy.orm import Session

from alpha_lab.alignment.alignment import AlignmentAssessment
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import CurrentExternalCalibration, CurrentMacroAssessment

_ALIGNMENT_PAGE_PATH = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "pages" / "7_Alignment.py"


def _import_alignment_page(db_path, monkeypatch):
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    spec = importlib.util.spec_from_file_location(
        f"dashboard_alignment_page_{db_path.name}", _ALIGNMENT_PAGE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assessment(*, market_as_of: date | None, donatien_retrieved_at: datetime | None) -> AlignmentAssessment:
    return AlignmentAssessment(
        as_of=date(2026, 1, 1), alignment="ALIGNED",
        market_regime="RISK_ON", market_regime_coverage=1.0, market_as_of=market_as_of,
        donatien_lean="CONSTRUCTIVE", donatien_dominant_regime="Test", donatien_confidence="High",
        donatien_defensiveness=0.5, donatien_scenario_weights={"Reacceleration": 1.0},
        donatien_run_date=date(2026, 1, 1), donatien_source_observed_at=donatien_retrieved_at,
        donatien_retrieved_at=donatien_retrieved_at,
    )


def _seed_current_macro(engine, *, as_of: date) -> None:
    with Session(engine) as session:
        session.add(CurrentMacroAssessment(
            scope="US", regime="RISK_ON", regime_score=1.0, confidence=1.0, coverage=1.0,
            content_hash="hash", methodology_version="v1", as_of=as_of, payload={},
        ))
        session.commit()


def _seed_current_calibration(engine, *, retrieved_at: datetime) -> None:
    with Session(engine) as session:
        session.add(CurrentExternalCalibration(
            source="Donatien", content_hash="hash", retrieved_at=retrieved_at,
            schema_version="v1", source_url="https://example.test",
            raw_payload={}, normalized_payload={},
        ))
        session.commit()


def test_staleness_reasons_flags_a_newer_macro_regime(tmp_path, monkeypatch):
    db_path = tmp_path / "alignment.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    _seed_current_macro(engine, as_of=date(2026, 1, 10))
    engine.dispose()

    module = _import_alignment_page(db_path, monkeypatch)
    try:
        assessment = _assessment(market_as_of=date(2026, 1, 1), donatien_retrieved_at=None)
        reasons = module._staleness_reasons(assessment, module.engine)
        assert any("Macro Regime" in reason for reason in reasons)
    finally:
        module.engine.dispose()


def test_staleness_reasons_flags_a_newer_donatien_calibration(tmp_path, monkeypatch):
    db_path = tmp_path / "alignment.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    _seed_current_calibration(engine, retrieved_at=datetime(2026, 1, 10, 12, 0))
    engine.dispose()

    module = _import_alignment_page(db_path, monkeypatch)
    try:
        assessment = _assessment(market_as_of=None, donatien_retrieved_at=datetime(2026, 1, 1, 12, 0))
        reasons = module._staleness_reasons(assessment, module.engine)
        assert any("Donatien" in reason for reason in reasons)
    finally:
        module.engine.dispose()


def test_staleness_reasons_empty_when_alignment_already_reflects_current_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "alignment.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    _seed_current_macro(engine, as_of=date(2026, 1, 1))
    _seed_current_calibration(engine, retrieved_at=datetime(2026, 1, 1, 12, 0))
    engine.dispose()

    module = _import_alignment_page(db_path, monkeypatch)
    try:
        assessment = _assessment(
            market_as_of=date(2026, 1, 1), donatien_retrieved_at=datetime(2026, 1, 1, 12, 0)
        )
        assert module._staleness_reasons(assessment, module.engine) == []
    finally:
        module.engine.dispose()


def test_staleness_reasons_empty_when_neither_source_has_ever_been_refreshed(tmp_path, monkeypatch):
    db_path = tmp_path / "alignment.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    engine.dispose()

    module = _import_alignment_page(db_path, monkeypatch)
    try:
        assessment = _assessment(market_as_of=None, donatien_retrieved_at=None)
        assert module._staleness_reasons(assessment, module.engine) == []
    finally:
        module.engine.dispose()
