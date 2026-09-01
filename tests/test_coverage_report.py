"""Focused tests for the read-only coverage reporting CLI."""

from copy import deepcopy
from datetime import date
import importlib.util
from io import StringIO
import json
from pathlib import Path

import pytest

from alpha_lab.screener import LiveResearchRecord

_SPEC = importlib.util.spec_from_file_location(
    "coverage_report", Path(__file__).parents[1] / "scripts" / "coverage_report.py"
)
assert _SPEC and _SPEC.loader
coverage_report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(coverage_report)
emit_report = coverage_report.emit_report
filter_records = coverage_report.filter_records
main = coverage_report.main
parse_args = coverage_report.parse_args


def _record(ticker: str, coverage: float) -> LiveResearchRecord:
    categories = {"earnings_growth": coverage, "valuation": 0.0}
    return LiveResearchRecord(
        ticker=ticker, company=ticker, price=1, market_cap=1, country="US",
        exchange="NASDAQ", sector=None, industry=None, asset_type="equity", themes=[],
        ethical_status="PASS", data_quality_status="valid", overall_score=None,
        category_scores={}, category_coverage=categories, raw_metrics={},
        percentile_metrics={}, overall_live_coverage=coverage,
        quantitative_coverage=coverage, ai_coverage=0, historical_coverage=0,
        confidence="Insufficient", provenance={}, last_refreshed=None,
        configuration_hash="fixture", evaluation_date=date(2026, 1, 1),
    )


def _run(records, **options):
    stdout, stderr = StringIO(), StringIO()
    emit_report(records, stdout=stdout, stderr=stderr, **options)
    return [json.loads(line) for line in stdout.getvalue().splitlines()], stderr.getvalue()


def test_default_mode_emits_records_then_scoped_summary_as_valid_json():
    output, errors = _run([_record("AAPL", .5), _record("ZERO", 0)])
    assert [item.get("ticker") for item in output[:-1]] == ["AAPL", "ZERO"]
    assert output[-1]["count"] == 2
    assert errors == ""


def test_help_exits_before_report_loader_runs():
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"], load_records=lambda: pytest.fail("loaded report records"))
    assert exit_info.value.code == 0


def test_unknown_argument_fails_clearly():
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--not-a-real-option"])
    assert exit_info.value.code == 2


def test_only_populated_filters_without_mutating_records():
    records = [_record("AAPL", .5), _record("ZERO", 0)]
    before = deepcopy(records)
    selected, missing = filter_records(records, only_populated=True)
    assert [record.ticker for record in selected] == ["AAPL"]
    assert missing == []
    assert records == before


def test_summary_only_suppresses_per_ticker_json():
    output, _ = _run([_record("AAPL", .5)], summary_only=True)
    assert output == [{
        "category_availability": {"earnings_growth": .5, "valuation": 0.0},
        "count": 1, "maximum": .5, "median": .5, "minimum": .5,
        "p25": .5, "p75": .5, "unavailable_reasons": {"valuation": 1},
    }]


def test_tickers_are_case_insensitive_deduplicated_and_requested_ordered():
    records = [_record("MSFT", .25), _record("AAPL", .5)]
    output, _ = _run(records, tickers=["aapl", "MSFT", "AAPL"])
    assert [item["ticker"] for item in output[:-1]] == ["AAPL", "MSFT"]
    assert output[-1]["count"] == 2


def test_combined_flags_scope_summary():
    records = [_record("AAPL", .5), _record("ZERO", 0), _record("MSFT", .25)]
    output, _ = _run(
        records, tickers=["zero", "aapl"], only_populated=True, summary_only=True
    )
    assert len(output) == 1
    assert output[0]["count"] == 1
    assert output[0]["median"] == .5


def test_unknown_ticker_is_reported_without_fabricating_a_record():
    output, errors = _run([_record("AAPL", .5)], tickers=["missing", "aapl"])
    assert [item.get("ticker") for item in output[:-1]] == ["AAPL"]
    assert errors == "Requested tickers not found: MISSING\n"


def test_empty_filtered_result_is_finite_valid_json():
    output, _ = _run(
        [_record("ZERO", 0)], only_populated=True, summary_only=True
    )
    assert output[0] == {
        "count": 0, "median": None, "p25": None, "p75": None,
        "minimum": None, "maximum": None, "category_availability": {},
        "unavailable_reasons": {},
    }
    encoded = json.dumps(output[0], allow_nan=False)
    assert "NaN" not in encoded and "Infinity" not in encoded
