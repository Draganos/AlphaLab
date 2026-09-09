"""Offline, deterministic tests for ExternalCalibrationService: current-state
persistence, historical hash-dedup, and provider-failure data preservation.
No network access."""

from datetime import UTC, datetime
import json

import pytest

from alpha_lab.calibration.service import DEFAULT_CALIBRATION_SOURCE, ExternalCalibrationService
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import ExternalCalibrationSnapshot
from alpha_lab.providers.donatien import (
    DonatienFetchResult,
    compute_content_hash,
    parse_donatien_payload,
)
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind

PAYLOAD = {
    "run_date": "2026-07-14",
    "run_time": "20:15",
    "macro_report_date": "2026-07-14",
    "supersedes": "PortfolioAnalyst-state-20260709.json",
    "dominant_regime": "Stagflation-lite - narrow lead",
    "confidence": "Medium",
    "scenario_weights": {"Stagflation": 35, "Soft Landing": 65},
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


class _FakeProvider:
    def __init__(self, payload: dict, *, fail: ProviderError | None = None):
        self._payload = payload
        self._fail = fail

    def fetch(self) -> DonatienFetchResult:
        if self._fail is not None:
            raise self._fail
        calibration = parse_donatien_payload(self._payload)
        return DonatienFetchResult(
            calibration=calibration,
            raw_payload=self._payload,
            content_hash=compute_content_hash(self._payload),
            retrieved_at=datetime.now(UTC),
            source_url="https://donatien.ca/fake",
        )


@pytest.fixture
def engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_first_refresh_creates_current_state_and_one_historical_snapshot(engine):
    service = ExternalCalibrationService(engine)
    current = service.refresh(_FakeProvider(PAYLOAD))
    assert current.source == DEFAULT_CALIBRATION_SOURCE
    assert current.content_hash == compute_content_hash(PAYLOAD)
    assert current.raw_payload == PAYLOAD
    assert current.normalized_payload["dominant_regime"] == PAYLOAD["dominant_regime"]
    assert current.supersedes == PAYLOAD["supersedes"]

    history = service.get_history()
    assert len(history) == 1
    assert history[0].content_hash == current.content_hash
    assert history[0].raw_payload == PAYLOAD


def test_unchanged_content_updates_current_state_but_creates_no_duplicate_snapshot(engine):
    service = ExternalCalibrationService(engine)
    service.refresh(_FakeProvider(PAYLOAD))
    first_retrieved_at = service.get_current().retrieved_at

    service.refresh(_FakeProvider(dict(PAYLOAD)))  # identical content, different object
    second = service.get_current()

    assert len(service.get_history()) == 1
    assert second.content_hash == compute_content_hash(PAYLOAD)
    # retrieved_at legitimately advances even though content didn't change --
    # this is "we checked again and it's still the same", not a no-op read.
    assert second.retrieved_at >= first_retrieved_at


def test_changed_content_creates_a_new_historical_snapshot(engine):
    service = ExternalCalibrationService(engine)
    service.refresh(_FakeProvider(PAYLOAD))

    changed = json.loads(json.dumps(PAYLOAD))
    changed["defensiveness"] = 5
    service.refresh(_FakeProvider(changed))

    history = service.get_history()
    assert len(history) == 2
    hashes = {row.content_hash for row in history}
    assert len(hashes) == 2
    current = service.get_current()
    assert current.content_hash == compute_content_hash(changed)


def test_failed_refresh_preserves_previous_valid_current_state_and_history(engine):
    service = ExternalCalibrationService(engine)
    service.refresh(_FakeProvider(PAYLOAD))
    before_current = service.get_current()
    before_history = service.get_history()

    failing = _FakeProvider(
        PAYLOAD, fail=ProviderError(ProviderErrorKind.INVALID_RESPONSE, "DonatienProvider", "schema failed")
    )
    with pytest.raises(ProviderError):
        service.refresh(failing)

    after_current = service.get_current()
    after_history = service.get_history()
    assert after_current.content_hash == before_current.content_hash
    assert after_current.retrieved_at == before_current.retrieved_at
    assert len(after_history) == len(before_history) == 1


def test_failed_refresh_on_first_ever_refresh_leaves_no_current_state(engine):
    """Distinct from the "already had valid data" case: a first refresh
    that fails must not fabricate a current row from nothing."""
    service = ExternalCalibrationService(engine)
    failing = _FakeProvider(
        PAYLOAD, fail=ProviderError(ProviderErrorKind.NETWORK_UNAVAILABLE, "DonatienProvider", "no network")
    )
    with pytest.raises(ProviderError):
        service.refresh(failing)
    assert service.get_current() is None
    assert service.get_history() == []


def test_historical_snapshot_row_has_no_update_mechanism(engine):
    """Immutability is structural: ExternalCalibrationSnapshot rows are only
    ever inserted by refresh(), never updated -- there is no method on
    ExternalCalibrationService that mutates an existing snapshot row."""
    service = ExternalCalibrationService(engine)
    service.refresh(_FakeProvider(PAYLOAD))
    public_methods = {name for name in dir(service) if not name.startswith("_")}
    assert public_methods == {
        "engine", "get_current", "get_current_calibration", "get_history",
        "get_calibration_as_of", "refresh",
    }


def test_get_current_calibration_returns_a_validated_donatien_calibration(engine):
    service = ExternalCalibrationService(engine)
    service.refresh(_FakeProvider(PAYLOAD))
    calibration = service.get_current_calibration()
    assert calibration is not None
    assert calibration.dominant_regime == PAYLOAD["dominant_regime"]
    assert calibration.tiers["Aggressive"].weights["Gold"].pct == 100


def test_get_current_and_get_history_return_none_or_empty_before_any_refresh(engine):
    service = ExternalCalibrationService(engine)
    assert service.get_current() is None
    assert service.get_current_calibration() is None
    assert service.get_history() == []


def test_snapshot_id_is_stable_across_identical_source_and_hash(engine):
    """Re-persisting an unchanged observation must be idempotent even across
    separate service instances / connections, not merely within one Python
    object's lifetime."""
    service_a = ExternalCalibrationService(engine)
    service_a.refresh(_FakeProvider(PAYLOAD))
    service_b = ExternalCalibrationService(engine)
    service_b.refresh(_FakeProvider(dict(PAYLOAD)))
    assert len(service_b.get_history()) == 1
