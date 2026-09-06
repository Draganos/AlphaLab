"""Deterministic, offline tests for the AlphaLab provider error boundary.

No test here ever depends on live Yahoo Finance -- every scenario mocks the
exception yfinance/curl_cffi/requests would raise and checks that
classification produces the right ProviderErrorKind.
"""
import json

import pytest
import requests.exceptions
import yfinance.exceptions as yf_exceptions

from alpha_lab.providers.errors import (
    ProviderError,
    ProviderErrorKind,
    call_with_classification,
    classify_yfinance_error,
)


def test_yf_rate_limit_error_is_classified_as_rate_limited():
    error = classify_yfinance_error(yf_exceptions.YFRateLimitError(), provider="YFinanceProvider")
    assert error.kind == ProviderErrorKind.RATE_LIMITED
    assert "rate-limited" in error.reason


def test_direct_http_429_status_is_classified_as_rate_limited():
    response = requests.Response()
    response.status_code = 429
    exc = requests.exceptions.HTTPError(response=response)
    error = classify_yfinance_error(exc, provider="YFinanceProvider")
    assert error.kind == ProviderErrorKind.RATE_LIMITED


def test_legacy_empty_body_json_decode_error_is_a_fallback_for_rate_limiting():
    """Reproduces the exact pre-upgrade symptom: yfinance 0.2.51 surfaced a
    raw JSONDecodeError with this message on a 429, instead of a clean
    exception. Even post-upgrade, this narrow pattern must still classify as
    rate limiting -- but only as a fallback, never the primary signal."""
    exc = json.JSONDecodeError("Expecting value", "", 0)
    error = classify_yfinance_error(exc, provider="YFinanceProvider")
    assert error.kind == ProviderErrorKind.RATE_LIMITED
    assert "fallback" in error.reason


def test_unrelated_json_decode_error_is_not_misclassified_as_rate_limited():
    """A malformed-but-non-empty response must not be force-fit into
    rate-limiting just because it is also a JSONDecodeError."""
    exc = json.JSONDecodeError("Unterminated string", '{"a": "b', 8)
    error = classify_yfinance_error(exc, provider="YFinanceProvider")
    assert error.kind == ProviderErrorKind.UNKNOWN_PROVIDER_ERROR


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("Failed to resolve 'query1.finance.yahoo.com'"),
        requests.exceptions.Timeout("timed out"),
        OSError("Name or service not known"),
    ],
)
def test_network_and_dns_failures_are_classified_as_network_unavailable_not_rate_limited(exc):
    error = classify_yfinance_error(exc, provider="YFinanceProvider")
    assert error.kind == ProviderErrorKind.NETWORK_UNAVAILABLE


def test_ticker_missing_error_is_classified_as_no_data():
    exc = yf_exceptions.YFTzMissingError("ZZZZ")
    error = classify_yfinance_error(exc, provider="YFinanceProvider")
    assert error.kind == ProviderErrorKind.NO_DATA


def test_unrecognized_exception_is_unknown_provider_error_not_fabricated_into_another_kind():
    error = classify_yfinance_error(ValueError("something odd"), provider="YFinanceProvider")
    assert error.kind == ProviderErrorKind.UNKNOWN_PROVIDER_ERROR


def test_provider_error_message_never_leaks_raw_exception_type_names_as_the_reason_alone():
    error = classify_yfinance_error(yf_exceptions.YFRateLimitError(), provider="YFinanceProvider")
    assert str(error) == "YFinanceProvider: Yahoo Finance rate-limited the request"


# --- call_with_classification: bounded retry behaviour -------------------


def test_call_with_classification_returns_result_on_success_without_retrying():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert call_with_classification(fn, provider="YFinanceProvider", backoff_seconds=0) == "ok"
    assert len(calls) == 1


def test_call_with_classification_retries_rate_limited_a_bounded_number_of_times_then_raises():
    calls = []

    def fn():
        calls.append(1)
        raise yf_exceptions.YFRateLimitError()

    with pytest.raises(ProviderError) as excinfo:
        call_with_classification(fn, provider="YFinanceProvider", max_retries=2, backoff_seconds=0)

    assert excinfo.value.kind == ProviderErrorKind.RATE_LIMITED
    # Exactly max_retries + 1 attempts: bounded, never an unlimited retry loop.
    assert len(calls) == 3


def test_call_with_classification_recovers_if_a_retry_succeeds():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 2:
            raise requests.exceptions.ConnectionError("timeout")
        return "recovered"

    assert call_with_classification(fn, provider="YFinanceProvider", backoff_seconds=0) == "recovered"
    assert len(calls) == 2


def test_call_with_classification_never_retries_no_data_or_unknown_errors():
    calls = []

    def fn():
        calls.append(1)
        raise yf_exceptions.YFTzMissingError("ZZZZ")

    with pytest.raises(ProviderError) as excinfo:
        call_with_classification(fn, provider="YFinanceProvider", max_retries=2, backoff_seconds=0)

    assert excinfo.value.kind == ProviderErrorKind.NO_DATA
    assert len(calls) == 1
