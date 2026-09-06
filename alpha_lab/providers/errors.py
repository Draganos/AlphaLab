"""AlphaLab-level provider error boundary.

``YFinanceProvider`` (and any future ``MarketDataProvider``) raises
``ProviderError`` instead of leaking yfinance/curl_cffi/requests internals
to ``IngestionService`` and callers like ``scripts/load_us_data.py``. Those
callers only ever need to know one of four things happened: the upstream
rate-limited us, the network/DNS was unavailable, the upstream genuinely has
no data for this ticker, or something unclassified went wrong -- never a
raw ``JSONDecodeError`` or ``curl_cffi`` exception type.

Classification never guesses: it inspects the concrete exception yfinance
actually raised, in priority order --

1. yfinance's own explicit exception (``YFRateLimitError``,
   ``YFTickerMissingError`` and its subclasses) -- the most reliable signal,
   since yfinance already parsed the response.
2. A direct HTTP status code on the exception's response, when present.
3. A narrow legacy-compatibility fallback: a ``JSONDecodeError`` whose
   message is the empty-body signature Yahoo returns on a 429
   (``"Expecting value"``) is still rate limiting, not a data problem --
   this is a fallback for older/alternate code paths, never the primary
   signal.

Anything that does not match one of the above becomes
``UNKNOWN_PROVIDER_ERROR`` rather than being force-fit into a bucket that
doesn't actually describe it.
"""

from enum import StrEnum
from typing import Callable, TypeVar
import json
import time

import yfinance.exceptions as yf_exceptions

T = TypeVar("T")


class ProviderErrorKind(StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    NO_DATA = "NO_DATA"
    UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"


# Only these kinds are worth a bounded retry: both describe a transient
# upstream condition. NO_DATA (ticker genuinely has nothing) and
# UNKNOWN_PROVIDER_ERROR (unclassified) would not be fixed by retrying.
RETRYABLE_KINDS = frozenset({ProviderErrorKind.RATE_LIMITED, ProviderErrorKind.NETWORK_UNAVAILABLE})

# yfinance 1.7.0 defaults YfConfig.network.retries to 0 (no internal retry
# loop at all), so a NETWORK_UNAVAILABLE failure reaches us having been
# attempted exactly once -- a short bounded retry here duplicates nothing.
# A RATE_LIMITED failure is different: yfinance already retries once
# internally (it swaps cookie strategy and re-requests before raising
# YFRateLimitError -- see yfinance/data.py's _make_request) before we ever
# see it, and retrying against an endpoint that just told us to back off
# only adds to the throttling. So rate limiting gets fewer retries than a
# plain network hiccup, not the same bound.
_DEFAULT_RETRY_LIMITS: dict[ProviderErrorKind, int] = {
    ProviderErrorKind.RATE_LIMITED: 1,
    ProviderErrorKind.NETWORK_UNAVAILABLE: 2,
}


class ProviderError(Exception):
    """Raised when an external provider call fails.

    Never raised for missing/malformed *data* the provider successfully
    retrieved -- that stays ``None``/empty per AlphaLab's evidence-integrity
    rule. This is strictly for the call itself failing.
    """

    def __init__(
        self,
        kind: ProviderErrorKind,
        provider: str,
        reason: str,
        *,
        cause: BaseException | None = None,
    ):
        self.kind = kind
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider}: {reason}")
        if cause is not None:
            self.__cause__ = cause


def classify_yfinance_error(exc: BaseException, *, provider: str) -> ProviderError:
    """Turn a raw exception raised by the yfinance call stack into a
    ``ProviderError`` with a stable, honest classification."""
    if isinstance(exc, yf_exceptions.YFRateLimitError):
        return ProviderError(
            ProviderErrorKind.RATE_LIMITED,
            provider,
            "Yahoo Finance rate-limited the request",
            cause=exc,
        )

    status_code = _response_status_code(exc)
    if status_code == 429:
        return ProviderError(
            ProviderErrorKind.RATE_LIMITED,
            provider,
            "Yahoo Finance rate-limited the request (HTTP 429)",
            cause=exc,
        )

    if isinstance(exc, json.JSONDecodeError) and "Expecting value" in str(exc):
        # Legacy-compatibility fallback only: Yahoo returns an empty body
        # with this exact decode signature when rate limiting some
        # endpoints. Not the primary signal -- YFRateLimitError and an
        # explicit 429 status are always checked first.
        return ProviderError(
            ProviderErrorKind.RATE_LIMITED,
            provider,
            "Yahoo Finance rate-limited the request (empty-body compatibility fallback)",
            cause=exc,
        )

    if isinstance(exc, yf_exceptions.YFTickerMissingError):
        return ProviderError(
            ProviderErrorKind.NO_DATA,
            provider,
            str(exc),
            cause=exc,
        )

    if isinstance(exc, yf_exceptions.YFException):
        return ProviderError(
            ProviderErrorKind.UNKNOWN_PROVIDER_ERROR,
            provider,
            str(exc) or type(exc).__name__,
            cause=exc,
        )

    if isinstance(exc, OSError):
        # Covers both requests' and curl_cffi's connection/timeout/DNS
        # exceptions -- both hierarchies derive their transport-level
        # errors (ConnectionError, Timeout, DNSError, socket.gaierror) from
        # OSError, regardless of which HTTP backend yfinance picked.
        return ProviderError(
            ProviderErrorKind.NETWORK_UNAVAILABLE,
            provider,
            f"Network unavailable while reaching Yahoo Finance ({type(exc).__name__})",
            cause=exc,
        )

    return ProviderError(
        ProviderErrorKind.UNKNOWN_PROVIDER_ERROR,
        provider,
        str(exc) or type(exc).__name__,
        cause=exc,
    )


def _response_status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def call_with_classification(
    fn: Callable[[], T],
    *,
    provider: str,
    max_retries: int | None = None,
    backoff_seconds: float = 1.0,
) -> T:
    """Run ``fn``, classifying any failure into a ``ProviderError``.

    Only retries a ``RATE_LIMITED``/``NETWORK_UNAVAILABLE`` classification,
    and only a small, fixed number of times with a short exponential
    backoff -- enough to ride out a single transient hiccup without turning
    one 429 into a flood of requests that makes the throttling worse. A
    ``NO_DATA`` or ``UNKNOWN_PROVIDER_ERROR`` classification is never
    retried: retrying would not change the outcome.

    ``max_retries=None`` (the default) uses ``_DEFAULT_RETRY_LIMITS``, which
    gives ``RATE_LIMITED`` fewer retries than ``NETWORK_UNAVAILABLE`` --
    yfinance already retries once internally before ever surfacing a rate
    limit to us, so piling on the same retry budget as a plain network
    hiccup would just add to the throttling. Pass an explicit value to
    override this for both kinds uniformly.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad; classified below
            error = classify_yfinance_error(exc, provider=provider)
            limit = _DEFAULT_RETRY_LIMITS.get(error.kind, 0) if max_retries is None else max_retries
            if error.kind not in RETRYABLE_KINDS or attempt >= limit:
                raise error from exc
            time.sleep(backoff_seconds * (2**attempt))
            attempt += 1
