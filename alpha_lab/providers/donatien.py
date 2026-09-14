"""Donatien External Calibration: a plain-HTTP fetch of a third-party market
regime/scenario snapshot, published as a machine-readable ``<pre
class="cal-json">`` block on an otherwise ordinary HTML page.

Architectural boundary (see project brief, Phase 1): Donatien is
EXTERNAL_CALIBRATION, not ground truth and not a stock-rating engine. This
module only fetches, validates, and normalizes the payload -- it has no
awareness of AlphaLab securities, scoring, or ranking, and nothing here is
wired into any of those systems. It is structurally identical in spirit to
``alpha_lab.providers.sec_edgar`` (a small, cached, retried, honestly-erroring
plain HTTP client) but talks to a single global page instead of a per-ticker
API family.

Schema note: the confirmed payload (see project brief) includes a
``supersedes`` field beyond the originally hypothesized field list. Every
field here is modeled exactly as observed in that confirmed payload --
nothing is invented. In particular:
- ``run_time`` has no seconds and no timezone; it is kept as the raw string
  and never assigned a timezone anywhere in this module.
- ``confidence`` and ``dominant_regime`` are free text, not closed enums --
  there is no evidence of their full vocabulary.
- ``defensiveness`` and ``top_drivers[].dominance`` are plain numbers with no
  documented scale in the payload itself; no range is asserted here.
- ``trend_contrarian_split`` values are single strings like ``"84/16"``, not
  two separate numeric fields -- stored verbatim, never split.
- ``tiers.<tier>.weights.<line>.gics_sector`` is present only for
  ``asset_class == "equity"`` rows; modeled as optional.

Schema revision (Phase 2G, live payload fetched and inspected directly --
see project brief): Donatien's live payload as of 2026-09-14 no longer
includes ``run_time``, ``confidence``, ``defensiveness``, ``top_drivers``,
``key_changes``, ``trend_contrarian_split``, or ``tiers.<tier>.expected_behaviour``
at all, and now includes three fields the original schema never saw:
top-level ``macro_report`` (a report filename) and ``note`` (free text), plus
per-tier ``trend_pct``/``contrarian_pct`` (the same trend/contrarian split
concept as the old ``trend_contrarian_split`` string, now pre-split into two
numbers) and a per-weight-line ``tag`` (``"trend"``/``"contra"``, no
documented full vocabulary -- modeled as free text like ``confidence``).
Every field above is now Optional (default ``None``) rather than required,
on both sides of the change, so a payload in either the original confirmed
shape or the current live shape validates successfully under one model --
this is a deliberate backwards-compatible schema (not a replacement),
because ``ExternalCalibrationService.get_current_calibration``/history
reads re-validate every previously persisted ``normalized_payload`` against
*this* model on every read, not just at write time; an old-shape row must
keep parsing after this change. ``extra="forbid"`` is deliberately kept at
every level: a field this module has never observed under either shape must
still fail loudly, never be silently accepted or dropped.
"""

from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import hashlib
import json
import time

from pydantic import BaseModel, ConfigDict, ValidationError

from alpha_lab.providers.errors import ProviderError, ProviderErrorKind

DONATIEN_METHODOLOGY_VERSION = "donatien-calibration-v1"

DEFAULT_DONATIEN_URL = "https://donatien.ca/members/reports/Portfolio/index.html"


# --- normalized, schema-validated representation ---------------------------


class DonatienTopDriver(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dominance: float


class DonatienTierWeightLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pct: float
    asset_class: str
    vehicle: str
    # Confirmed present only for equity rows in the observed payload; never
    # fabricated for commodity/fixed_income/cash rows.
    gics_sector: str | None = None
    # Present only in the current (2026-09-14+) live schema -- free text
    # ("trend"/"contra" observed), no documented full vocabulary, same
    # honesty convention as `confidence`/`dominant_regime` below.
    tag: str | None = None


class DonatienTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Present only in the original confirmed schema; absent from the current
    # live payload (see module-level schema-revision note). None, never a
    # fabricated placeholder, when the source didn't supply it.
    expected_behaviour: str | None = None
    weights: dict[str, DonatienTierWeightLine]
    # Present only in the current (2026-09-14+) live schema -- the same
    # trend/contrarian split concept the original schema expressed as a
    # single top-level string per tier (e.g. "84/16"), now pre-split into
    # two numbers. Never derived from each other; each is exactly what the
    # source reported for that field, or None if the source didn't supply it.
    trend_pct: float | None = None
    contrarian_pct: float | None = None


class DonatienCalibration(BaseModel):
    """One validated Donatien observation. Structural passthrough only --
    AlphaLab derives no score, rating, or stock-level meaning from this.

    ``extra="forbid"`` at every level: an unrecognized field anywhere is a
    schema change AlphaLab has not verified, and must fail loudly rather
    than being silently dropped.

    Every field below except `run_date`/`macro_report_date`/`dominant_regime`/
    `scenario_weights`/`tiers` is Optional: this model accepts both the
    original confirmed payload shape and the current (2026-09-14+) live
    shape (see the schema-revision note above the class definitions in this
    module) under one schema, because previously persisted rows are
    re-validated against this exact model on every read
    (`ExternalCalibrationService.get_current_calibration`/`get_history`) --
    a row written under either shape must keep parsing after this file
    changes. A field being `None` here means the source did not report it
    under whichever shape produced this observation; it is never inferred
    from the other shape's equivalent field.
    """

    model_config = ConfigDict(extra="forbid")

    run_date: date
    # Present only in the original confirmed schema; absent from the current
    # live payload. No seconds/timezone even when present -- kept as the raw
    # string, never assigned one.
    run_time: str | None = None
    macro_report_date: date
    supersedes: str | None = None
    dominant_regime: str
    # Present only in the original confirmed schema.
    confidence: str | None = None
    scenario_weights: dict[str, float]
    # Present only in the original confirmed schema.
    defensiveness: float | None = None
    # Present only in the original confirmed schema.
    top_drivers: list[DonatienTopDriver] | None = None
    # Present only in the original confirmed schema.
    key_changes: list[str] | None = None
    # Present only in the original confirmed schema; superseded in the
    # current live schema by per-tier `trend_pct`/`contrarian_pct` above.
    trend_contrarian_split: dict[str, str] | None = None
    tiers: dict[str, DonatienTier]
    # Present only in the current (2026-09-14+) live schema: a report
    # filename reference and free-text note, both previously absent.
    macro_report: str | None = None
    note: str | None = None


def parse_donatien_payload(raw_payload: dict[str, Any]) -> DonatienCalibration:
    """Pure, deterministic parse + validate of an already-JSON-decoded
    payload. Raises ``pydantic.ValidationError`` on any missing field,
    unexpected field, or type mismatch -- never coerces or guesses."""
    return DonatienCalibration.model_validate(raw_payload)


def source_observed_at(calibration: DonatienCalibration) -> datetime | None:
    """Best-effort convenience combination of run_date + run_time into one
    naive datetime, for sorting/display only.

    Seconds are 0 because the source never supplies them -- that is the
    coarsest faithful reading of "20:15", not fabricated precision. No
    timezone is attached, because none is established by the source. Returns
    None if run_time isn't parseable as HH:MM, rather than guessing.
    """
    try:
        hour_str, minute_str = calibration.run_time.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime(
        calibration.run_date.year, calibration.run_date.month, calibration.run_date.day,
        hour, minute,
    )


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def compute_content_hash(raw_payload: dict[str, Any]) -> str:
    """SHA-256 over the exact raw Donatien payload. Every field in this
    payload is substantive source content -- unlike AlphaLab-generated
    snapshots elsewhere, there is no AlphaLab-added volatile timestamp field
    to exclude before hashing."""
    return hashlib.sha256(_canonical_json(raw_payload).encode()).hexdigest()


# --- HTML extraction --------------------------------------------------------


class _CalJsonExtractor(HTMLParser):
    """Extracts the text content of the first ``<pre class="cal-json">``
    element. Deliberately narrow: this never parses or trusts any other page
    content."""

    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self.found = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "pre" or self.found:
            return
        classes = (dict(attrs).get("class") or "").split()
        if "cal-json" in classes:
            self._capturing = True
            self.found = True

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre" and self._capturing:
            self._capturing = False

    def text(self) -> str:
        return "".join(self._parts).strip()


def extract_cal_json(html_text: str) -> str:
    """Return the raw text inside ``<pre class="cal-json">...</pre>``.

    Raises ``ValueError`` if the container is not found -- callers must
    treat that as a failed fetch, never as "no data" to silently accept.
    """
    parser = _CalJsonExtractor()
    parser.feed(html_text)
    if not parser.found:
        raise ValueError("'<pre class=\"cal-json\">' container not found on page")
    text = parser.text()
    if not text:
        raise ValueError("'<pre class=\"cal-json\">' container was empty")
    return text


# --- provider ----------------------------------------------------------------


class DonatienFetchResult(BaseModel):
    """Everything one successful fetch produces, before any persistence
    decision is made."""

    calibration: DonatienCalibration
    raw_payload: dict[str, Any]
    content_hash: str
    retrieved_at: datetime
    source_url: str


class DonatienProvider:
    """Plain HTTP fetch of the Donatien calibration page, following the same
    shape as ``alpha_lab.providers.sec_edgar.SECClient``: explicit timeout,
    an honest identifying User-Agent, bounded retries on transport failures
    only, and a classified ``ProviderError`` on any failure -- never a raw
    ``urllib``/``json``/pydantic exception leaking to callers.

    Authentication: UNVERIFIED. The confirmed payload establishes the
    content/schema but not whether the live endpoint requires a session --
    this class implements a plain, unauthenticated GET consistent with the
    documented extraction path. If the live page actually requires login, a
    fetch will correctly fail with INVALID_RESPONSE (missing cal-json
    container) rather than silently "succeeding" against a login page.
    """

    provider_name = "DonatienProvider"

    def __init__(
        self,
        url: str = DEFAULT_DONATIEN_URL,
        *,
        timeout: float = 30.0,
        retries: int = 2,
        user_agent: str = "AlphaLab research contact@example.invalid",
    ):
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent

    def fetch(self) -> DonatienFetchResult:
        """Fetch -> extract -> parse -> validate. Raises ``ProviderError``
        on any failure (network, missing container, malformed JSON, or
        schema validation) -- never returns a partially valid result."""
        html_text = self._fetch_html()
        retrieved_at = datetime.now(UTC)

        try:
            raw_json_text = extract_cal_json(html_text)
        except ValueError as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, self.provider_name, str(exc), cause=exc
            ) from exc

        try:
            raw_payload = json.loads(raw_json_text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                self.provider_name,
                f"cal-json payload is not valid JSON: {exc}",
                cause=exc,
            ) from exc

        if not isinstance(raw_payload, dict):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                self.provider_name,
                f"cal-json payload must be a JSON object, got {type(raw_payload).__name__}",
            )

        try:
            calibration = parse_donatien_payload(raw_payload)
        except ValidationError as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                self.provider_name,
                f"cal-json schema validation failed: {exc}",
                cause=exc,
            ) from exc

        return DonatienFetchResult(
            calibration=calibration,
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            retrieved_at=retrieved_at,
            source_url=self.url,
        )

    def _fetch_html(self) -> str:
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(self.url, headers={"User-Agent": self.user_agent})
                with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    return response.read().decode("utf-8", errors="replace")
            except (HTTPError, URLError, TimeoutError) as exc:
                error = exc
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
        raise ProviderError(
            ProviderErrorKind.NETWORK_UNAVAILABLE,
            self.provider_name,
            f"failed to fetch Donatien calibration page: {error}",
            cause=error,
        )
