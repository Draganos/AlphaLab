"""Offline, deterministic tests for the Donatien payload model, HTML
extraction, and provider. No network access."""

from datetime import date, datetime
import json

import pytest
from pydantic import ValidationError
from urllib.error import HTTPError, URLError

from alpha_lab.providers import donatien as donatien_module
from alpha_lab.providers.donatien import (
    DonatienProvider,
    compute_content_hash,
    extract_cal_json,
    parse_donatien_payload,
    source_observed_at,
)
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind

CONFIRMED_PAYLOAD = {
    "run_date": "2026-07-14",
    "run_time": "20:15",
    "macro_report_date": "2026-07-14",
    "supersedes": "PortfolioAnalyst-state-20260709.json",
    "dominant_regime": "Stagflation-lite - narrow lead (energy-supply-shock driven)",
    "confidence": "Medium",
    "scenario_weights": {
        "Stagflation": 35,
        "Soft Landing": 27,
        "Reacceleration": 21,
        "Deflationary Bust": 17,
    },
    "defensiveness": 4,
    "top_drivers": [
        {"name": "Iran/Hormuz Crisis", "dominance": 5},
        {"name": "Global Monetary Policy", "dominance": 5},
    ],
    "key_changes": ["Same defensiveness (+4) but composition rotates"],
    "trend_contrarian_split": {"Aggressive": "84/16", "Balanced": "91/9", "Conservative": "95/5"},
    "tiers": {
        "Aggressive": {
            "expected_behaviour": "Leads in Reacceleration/energy-shock paths.",
            "weights": {
                "Energy": {"pct": 11, "asset_class": "equity", "vehicle": "XLE", "gics_sector": "Energy"},
                "Gold": {"pct": 11, "asset_class": "commodity", "vehicle": "GLD"},
            },
        },
    },
}


def _payload(**overrides):
    payload = json.loads(json.dumps(CONFIRMED_PAYLOAD))
    payload.update(overrides)
    return payload


# --- payload / schema --------------------------------------------------------


def test_confirmed_payload_parses_successfully():
    calibration = parse_donatien_payload(CONFIRMED_PAYLOAD)
    assert calibration.run_date == date(2026, 7, 14)
    assert calibration.run_time == "20:15"
    assert calibration.supersedes == "PortfolioAnalyst-state-20260709.json"
    assert calibration.scenario_weights["Stagflation"] == 35
    assert calibration.tiers["Aggressive"].weights["Gold"].gics_sector is None
    assert calibration.tiers["Aggressive"].weights["Energy"].gics_sector == "Energy"


def test_missing_required_field_fails_schema_validation():
    payload = _payload()
    del payload["dominant_regime"]
    with pytest.raises(ValidationError):
        parse_donatien_payload(payload)


def test_unexpected_top_level_field_fails_schema_validation():
    payload = _payload(unexpected_new_field="surprise")
    with pytest.raises(ValidationError):
        parse_donatien_payload(payload)


def test_wrong_type_fails_schema_validation():
    payload = _payload(defensiveness="very defensive")
    with pytest.raises(ValidationError):
        parse_donatien_payload(payload)


def test_malformed_scenario_weights_fails_schema_validation():
    payload = _payload(scenario_weights={"Stagflation": "high"})
    with pytest.raises(ValidationError):
        parse_donatien_payload(payload)


def test_malformed_driver_fails_schema_validation():
    payload = _payload(top_drivers=[{"name": "X"}])  # missing dominance
    with pytest.raises(ValidationError):
        parse_donatien_payload(payload)


def test_malformed_nested_tier_fails_schema_validation():
    payload = _payload()
    payload["tiers"]["Aggressive"]["weights"]["Energy"] = {"pct": 11}  # missing asset_class/vehicle
    with pytest.raises(ValidationError):
        parse_donatien_payload(payload)


def test_unexpected_structure_top_level_not_an_object():
    with pytest.raises(ValidationError):
        parse_donatien_payload(["not", "an", "object"])  # type: ignore[arg-type]


# --- HTML extraction ---------------------------------------------------------


def test_extract_cal_json_finds_the_container():
    html = f'<html><body><pre class="cal-json">{json.dumps(CONFIRMED_PAYLOAD)}</pre></body></html>'
    extracted = extract_cal_json(html)
    assert json.loads(extracted) == CONFIRMED_PAYLOAD


def test_extract_cal_json_raises_when_container_missing():
    with pytest.raises(ValueError, match="not found"):
        extract_cal_json("<html><body><p>Please log in</p></body></html>")


def test_extract_cal_json_raises_when_container_empty():
    with pytest.raises(ValueError, match="empty"):
        extract_cal_json('<html><body><pre class="cal-json"></pre></body></html>')


def test_extract_cal_json_ignores_unrelated_pre_blocks():
    html = '<pre>irrelevant</pre><pre class="cal-json">{"a": 1}</pre>'
    assert json.loads(extract_cal_json(html)) == {"a": 1}


# --- hashing ------------------------------------------------------------------


def test_content_hash_is_stable_for_identical_payload():
    assert compute_content_hash(CONFIRMED_PAYLOAD) == compute_content_hash(dict(CONFIRMED_PAYLOAD))


def test_content_hash_changes_when_payload_changes():
    changed = _payload(defensiveness=5)
    assert compute_content_hash(CONFIRMED_PAYLOAD) != compute_content_hash(changed)


# --- timestamp semantics -------------------------------------------------------


def test_source_observed_at_combines_date_and_time_without_timezone():
    calibration = parse_donatien_payload(CONFIRMED_PAYLOAD)
    observed = source_observed_at(calibration)
    assert observed == datetime(2026, 7, 14, 20, 15)
    assert observed.tzinfo is None
    assert observed.second == 0


def test_source_observed_at_returns_none_for_unparseable_time():
    payload = _payload(run_time="not-a-time")
    calibration = parse_donatien_payload(payload)
    assert source_observed_at(calibration) is None


# --- provider: HTML/HTTP failure classification --------------------------------


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _html_for(payload: dict) -> bytes:
    return f'<pre class="cal-json">{json.dumps(payload)}</pre>'.encode()


def test_provider_fetch_succeeds_with_a_valid_confirmed_payload(monkeypatch):
    monkeypatch.setattr(
        donatien_module, "urlopen", lambda *a, **k: _FakeHTTPResponse(_html_for(CONFIRMED_PAYLOAD))
    )
    provider = DonatienProvider("https://donatien.ca/fake")
    result = provider.fetch()
    assert result.calibration.dominant_regime.startswith("Stagflation-lite")
    assert result.content_hash == compute_content_hash(CONFIRMED_PAYLOAD)
    assert result.raw_payload == CONFIRMED_PAYLOAD


def test_provider_fetch_raises_network_unavailable_on_http_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise HTTPError("https://donatien.ca/fake", 500, "boom", None, None)

    monkeypatch.setattr(donatien_module, "urlopen", _raise)
    provider = DonatienProvider("https://donatien.ca/fake", retries=0)
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch()
    assert excinfo.value.kind == ProviderErrorKind.NETWORK_UNAVAILABLE


def test_provider_fetch_raises_network_unavailable_on_timeout(monkeypatch):
    def _raise(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(donatien_module, "urlopen", _raise)
    provider = DonatienProvider("https://donatien.ca/fake", retries=0)
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch()
    assert excinfo.value.kind == ProviderErrorKind.NETWORK_UNAVAILABLE


def test_provider_fetch_raises_network_unavailable_on_url_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise URLError("no route to host")

    monkeypatch.setattr(donatien_module, "urlopen", _raise)
    provider = DonatienProvider("https://donatien.ca/fake", retries=0)
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch()
    assert excinfo.value.kind == ProviderErrorKind.NETWORK_UNAVAILABLE


def test_provider_fetch_raises_invalid_response_when_cal_json_missing(monkeypatch):
    monkeypatch.setattr(
        donatien_module, "urlopen", lambda *a, **k: _FakeHTTPResponse(b"<html>please log in</html>")
    )
    provider = DonatienProvider("https://donatien.ca/fake")
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch()
    assert excinfo.value.kind == ProviderErrorKind.INVALID_RESPONSE


def test_provider_fetch_raises_invalid_response_on_malformed_json(monkeypatch):
    monkeypatch.setattr(
        donatien_module,
        "urlopen",
        lambda *a, **k: _FakeHTTPResponse(b'<pre class="cal-json">{not valid json</pre>'),
    )
    provider = DonatienProvider("https://donatien.ca/fake")
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch()
    assert excinfo.value.kind == ProviderErrorKind.INVALID_RESPONSE


def test_provider_fetch_raises_invalid_response_on_schema_validation_failure(monkeypatch):
    broken = _payload()
    del broken["dominant_regime"]
    monkeypatch.setattr(donatien_module, "urlopen", lambda *a, **k: _FakeHTTPResponse(_html_for(broken)))
    provider = DonatienProvider("https://donatien.ca/fake")
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch()
    assert excinfo.value.kind == ProviderErrorKind.INVALID_RESPONSE


def test_provider_fetch_raises_invalid_response_when_payload_is_not_an_object(monkeypatch):
    monkeypatch.setattr(
        donatien_module, "urlopen", lambda *a, **k: _FakeHTTPResponse(b'<pre class="cal-json">[1, 2, 3]</pre>')
    )
    provider = DonatienProvider("https://donatien.ca/fake")
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch()
    assert excinfo.value.kind == ProviderErrorKind.INVALID_RESPONSE
