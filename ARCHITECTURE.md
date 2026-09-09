# AlphaLab Architecture

This document describes the architecture as actually implemented. Where a
future component is discussed, it is explicitly labeled **FUTURE / NOT
IMPLEMENTED** — nothing in this file describes aspirational design as if it
already existed.

## 1. Overview

```text
                     AlphaLab
                        │
        ┌───────────────┴────────────────┐
        │                                │
 Existing Research                  External Data
        │                                │
 Fundamental                         Donatien
 Analyst Consensus                   Calibration
 Technical Summary                       │
 AI Research Rating                      │
        │                                │
        └───────────────┬────────────────┘
                        │
                 Stored Evidence
                        │
                 [Phase 1 boundary]
                        │
              No scoring integration
```

AlphaLab is organized into two kinds of evidence today:

- **Existing Research** (`alpha_lab.research`, `alpha_lab.strategy`,
  `alpha_lab.screener`, `alpha_lab.backtest`, `alpha_lab.portfolio`,
  `alpha_lab.ratings`, `alpha_lab.factors`) — the fundamental score, Analyst
  Consensus, Technical Summary, and AI Research Rating. This is the
  system's stable core; nothing in this document changes its behavior.
- **External Calibration** (`alpha_lab.calibration`,
  `alpha_lab.providers.donatien`) — a single third-party market
  regime/scenario snapshot (Donatien), added in Phase 1. It is fetched,
  validated, timestamped, hashed, persisted, and displayed. **It has no
  code path into any scoring or ranking calculation** — see §6.
- **AlphaLab Macro Regime** (`alpha_lab.macro`) — a deterministic,
  AlphaLab-computed regime read derived from market-observable proxy
  prices already ingested into AlphaLab's own `Price` table (Phase 2, step
  1). **Market-derived proxies only — never official economic data, and no
  code path into any scoring or ranking calculation** — see §16.
- **Alignment** (`alpha_lab.alignment`) — a small, categorical-only
  comparison of AlphaLab Macro Regime against Donatien External
  Calibration (Phase 2, step 2 / "Phase 2B"). Produces one of
  `ALIGNED`/`CONFLICT`/`NEUTRAL`/`INSUFFICIENT_DATA` — **no numeric score,
  and no code path into any scoring or ranking calculation** — see §17.

## 2. Deterministic components

These are pure, reproducible computations. Given the same inputs and
methodology version, they always produce the same output — no LLM
involvement.

| Component | Inputs | Outputs | Persistence | Timestamps | Affects ranking? |
|---|---|---|---|---|---|
| Ingestion/normalization (`alpha_lab.ingestion`) | Raw provider responses (yfinance, SEC, Nasdaq Trader) | `Security`/`Price`/`Fundamental`/`Estimate`/`SECCompanyFact` rows | Additive SQL tables, hash-deduplicated | `ingested_at`, `publication_date`/`filed_date` | Yes (upstream of scoring) |
| Security identity | Ticker string | Normalized ticker (`.strip().upper()`, applied ad hoc per module — no single canonical resolver exists yet) | `securities` table, ticker PK | `metadata_updated_at` | Yes |
| Fundamental scoring (`alpha_lab.strategy.scoring`, `alpha_lab.factors`) | Price/fundamental factors | `CompositeResult`/`HistoricalScore` (0–100 score) | `factor_scores`, `current_research_snapshots` | `generated_at`, `evaluation_date` | **Yes — the core ranking signal** |
| Analyst Consensus (`alpha_lab.research.analyst_consensus`) | yfinance recommendation/target data | `AnalystConsensus` (rating, counts, price targets) | `current_analyst_consensus` (current), `research_snapshots` (historical, embedded in `StockResearch`) | `as_of`, `computed_at` | No — separate research output |
| Technical Summary (`alpha_lab.research.technical`) | AlphaLab's own stored `Price` history | `TechnicalSummary` (15 indicators, rollup rating) | `current_technical_summary` | `as_of`, `computed_at` | No |
| Macro Regime (`alpha_lab.macro.regime`) | AlphaLab's own stored `Price` history for 6 market-proxy tickers (^VIX, ^TNX, ^IRX, DX-Y.NYB, CL=F, GC=F) | `MacroAssessment` (regime, 5 indicators, coverage) | `current_macro_assessment`, `macro_assessment_snapshots` | `as_of`, `computed_at` | **No — market-derived proxies only, never official economic data, no scoring authority** |
| Calibration parsing (`alpha_lab.providers.donatien`) | Donatien's `<pre class="cal-json">` HTML block | `DonatienCalibration` (validated, normalized) | `current_external_calibration`, `external_calibration_snapshots` | `source_observed_at`, `retrieved_at` | **No — zero scoring authority in Phase 1** |
| Alignment (`alpha_lab.alignment.alignment`) | Already-computed `MacroAssessment` + `DonatienCalibration` (no network) | `AlignmentAssessment` (categorical `alignment` + audit fields, no score) | `current_alignment_assessment`, `alignment_assessment_snapshots` | `as_of`, `computed_at` | **No — categorical comparison only, no scoring authority** |
| CalibrationAlignment (`alpha_lab.calibration.sector_alignment`) | `Security.sector` (yfinance/Morningstar) + already-stored `CurrentExternalCalibration` (no network) | `list[SectorTierWeight]` (Donatien's own published `pct` per matching tier line) | None — recomputed fresh on every call | n/a (no persistence) | **No — data connection only, no verdict, no scoring authority** |
| News metadata | — | **FUTURE / NOT IMPLEMENTED** (`ResearchNewsProvider` interface exists; no implementation) | — | — | — |
| Backtesting (`alpha_lab.backtest`) | `HistoricalScoringService` output | `BacktestResult` (Sharpe, drawdown, turnover, ...) | `backtest_runs` | `created_at` | Yes (evaluates ranking, doesn't rank live) |

## 3. AI-derived components

These use an LLM (or a deterministic rule-based stand-in) to produce
*qualitative* judgments; all numeric scores derived from them are computed
by deterministic Python, never by the model itself.

| Component | Role | Deterministic scoring layer | Independence guarantee |
|---|---|---|---|
| Legacy document-commentary AI (`alpha_lab.ai`) | Summarizes company documents into qualitative scores | `alpha_lab.screener.service` blends this into the fundamental score's `ai_research` category | Feeds the fundamental score directly (pre-existing, unchanged) |
| AI Research Rating (`alpha_lab.research.ai_rating`) | Synthesizes Fundamental + Analyst Consensus + Technical Summary evidence into 6 dimension judgments | `compute_score`/`compute_confidence`/`_meets_minimum_evidence` (pure Python) | Explicitly excludes the legacy `ai_research` category from its own evidence (`_EXCLUDED_CATEGORIES`) to avoid AI-on-AI circularity; never blended into the fundamental score |

Donatien has **no AI-derived component** in Phase 1 — it is a structural
passthrough of validated source data only.

## 4. External components

| Component | Source | Trust status | Phase 1 usage |
|---|---|---|---|
| Donatien External Calibration | `https://donatien.ca/members/reports/Portfolio/index.html`, a `<pre class="cal-json">` block | `EXTERNAL_CALIBRATION` — explicitly **not** ground truth | Fetch → validate → persist → display only |

## 5. Future components — NOT IMPLEMENTED

Listed for context only; none of the following exist in code today, and
nothing in this phase built toward them beyond leaving the door open (e.g.
Donatien's stored timestamps are structured so a future point-in-time
integration is possible, without that integration existing yet):

- **News Engine** — no ingestion, no model, no provider implementation.
  `alpha_lab.providers.interfaces.ResearchNewsProvider` is an abstract
  interface with zero implementations.
- **Full Macro Engine** — `alpha_lab.macro` (§16) implements a narrow,
  market-proxy-only regime read (VIX, yield curve, USD, oil, gold via
  yfinance). It does NOT implement official economic data (CPI, GDP,
  employment via FRED or similar), multi-region regimes, or any comparison
  against Donatien's own regime label (§10 of the original brief) — that
  remains future work.
- **Alignment** (`alpha_lab.alignment`, §17) is now implemented, but only a
  narrow slice: a two-input, categorical-only comparison of Market Regime
  vs. Donatien's `scenario_weights`. **CalibrationAlignment
  (security-level)** (`alpha_lab.calibration.sector_alignment`, §18) is
  also now implemented, but only as a read-only data connection (which
  Donatien tier weight lines reference a security's approximate sector,
  at Donatien's own published weight) — not a categorical verdict, and not
  built on a real GICS taxonomy (see §18's disclosed approximation). The
  following remain **NOT IMPLEMENTED**:
  - **NewsImpact** — a future classification of news events against
    existing theses/calibration.
  - **Full Conviction Layer** — a cross-domain agreement/conflict
    assessment spanning more than Market Regime + Donatien (e.g. adding
    News, Analyst Consensus, or AI Research Rating as further inputs).
    §17's Alignment is a first, deliberately narrow instance of this
    concept — not the full layer — and per the project brief must never
    become a simple average when the fuller version is built.
  - **Signal Conflict Detection** beyond §17's two-input case.
- **Ranking integration for Donatien/News/Macro/Alignment** — not
  implemented, and must not be added without empirical backtest validation
  per the project brief.

## 6. Provider boundary

`alpha_lab.providers.donatien.DonatienProvider` follows the same shape as
`alpha_lab.providers.sec_edgar.SECClient` (the established "plain HTTP
fetch" template in this codebase): explicit timeout, an honest identifying
`User-Agent`, bounded retries on transport failures only, and a classified
`ProviderError` on any failure — never a raw `urllib`/`json`/pydantic
exception reaching a caller.

```text
HTTP fetch (urllib, explicit timeout, bounded retries)
    ↓
extract <pre class="cal-json"> (fails loudly if missing/empty)
    ↓
json.loads (fails loudly if malformed)
    ↓
pydantic schema validation, extra="forbid" (fails loudly on
    missing/unexpected fields or wrong types)
    ↓
DonatienFetchResult (validated calibration + raw payload + content
    hash + retrieved_at)
```

**Authentication: unverified.** The confirmed payload establishes content
and schema but not whether the live endpoint requires a session — this
environment could not reach `donatien.ca` at all (organizational network
policy). The provider implements a plain, unauthenticated GET consistent
with the documented extraction path; if the live page actually requires
login, a fetch correctly fails with `ProviderErrorKind.INVALID_RESPONSE`
(missing `cal-json` container) rather than silently succeeding against a
login page.

## 7. Persistence boundary

Two tables, following the existing `Current*` / immutable-snapshot split
used throughout this codebase (e.g. `CurrentAnalystConsensus` /
`ResearchSnapshot`):

- **`current_external_calibration`** — one row per source (`source` is the
  primary key; always `"Donatien"` today). Upserted only by an explicit
  refresh. A failed refresh leaves this row untouched.
- **`external_calibration_snapshots`** — immutable, append-only. A new row
  is inserted only when the content hash changes; re-fetching identical
  content never creates a duplicate (`snapshot_id` is a deterministic
  `sha256(source, content_hash)`, matching `ResearchSnapshot`'s identity
  pattern).

Both tables store the complete raw payload (`raw_payload`, exactly as
received) and the validated normalized representation
(`normalized_payload`), the content hash, `source_url`, and the AlphaLab
methodology/schema version (`DONATIEN_METHODOLOGY_VERSION`) — never only a
derived interpretation.

Schema creation is additive-only via
`alpha_lab.database.session.create_schema` (`Base.metadata.create_all` +
column-level `ALTER TABLE IF NOT EXISTS` checks for pre-existing tables).
No Alembic, no separate migration framework — consistent with every other
table in this codebase.

## 8. Current vs. historical state

Same pattern as the rest of AlphaLab: a mutable "current" row answers "what
do we believe right now," and an immutable historical table answers "what
did we observe and when." Reads of current state never touch history, and
history is never rewritten to reflect a later refresh.

## 9. Timestamp semantics

Three timestamps are kept distinct, never substituted for one another:

| Field | Meaning | Note |
|---|---|---|
| `source_run_date` / `source_run_time_raw` | Donatien's own `run_date`/`run_time`, verbatim | `run_time` has no seconds and no timezone in the source; stored as the raw string, never assigned one |
| `source_observed_at` | Best-effort naive combination of the two above | Convenience only; seconds are 0 because the source never supplies them, and `tzinfo` is `None` because none is established — not fabricated precision |
| `retrieved_at` | When AlphaLab's provider fetched the page (UTC, timezone-aware) | Never presented as if it were the source's own observation time |
| `created_at` (`external_calibration_snapshots`) / `updated_at` (`current_external_calibration`) | When AlphaLab persisted the row | Distinct from both of the above, mirroring `ResearchSnapshot.created_at` vs `.evaluation_date` |

## 10. Content hashing

`alpha_lab.providers.donatien.compute_content_hash` follows this
codebase's universal convention: `sha256(json.dumps(payload, sort_keys=True,
separators=(",", ":")))`. Unlike `ResearchSnapshot._payload_hash` (which
excludes AlphaLab's own `generated_at` field before hashing), the Donatien
hash includes the *entire* raw payload — every field in it (including
`run_date`/`run_time`) is substantive Donatien content, not an
AlphaLab-added volatile field.

## 11. Refresh flow

```text
Explicit refresh (script or UI button click)
      ↓
DonatienProvider.fetch()   -- one HTTP request, never from a page render
      ↓
ExternalCalibrationService.refresh()  -- validate, hash-compare, write
      ↓
current_external_calibration / external_calibration_snapshots
      ↓
Streamlit reads stored state (app/dashboard/pages/5_External_Calibration.py)
```

`scripts/refresh_donatien_calibration.py` is the batch/scheduled entrypoint.
The Streamlit page's "Refresh calibration" button is the interactive
equivalent — both call the same `ExternalCalibrationService.refresh`.
Opening the page, or any other page, **never** triggers a Donatien fetch.

## 12. Failure behavior

`DonatienProvider.fetch()` raises `ProviderError` (never a raw exception)
for every failure mode: HTTP/network/timeout failures classify as
`ProviderErrorKind.NETWORK_UNAVAILABLE`; a missing `cal-json` container,
malformed JSON, or schema/type validation failure all classify as the new
`ProviderErrorKind.INVALID_RESPONSE` (added in this phase — the existing
four-kind enum had no member for "got a response, but its content is
unusable"). In every case, the provider call happens entirely before any
database write, so a failed refresh leaves the previous valid current state
and all history completely untouched.

## 13. UI read path

`app/dashboard/pages/5_External_Calibration.py` is a new, dedicated page
("External Calibration / Market Regime") — not part of the Market Screener
table, since Donatien is global market context, not per-security research.
It only reads persisted state (`ExternalCalibrationService.get_current` /
`.get_history`); it makes zero network calls on render. The one exception —
consistent with the existing "Refresh for this ticker" button on the
Company Research page — is an explicit, user-clicked "Refresh calibration"
button.

## 14. Explicit non-integration with existing scoring (Phase 1 boundary)

Nothing in `alpha_lab.calibration` or `alpha_lab.providers.donatien` is
imported by `alpha_lab.research`, `alpha_lab.screener`, `alpha_lab.strategy`,
`alpha_lab.backtest`, `alpha_lab.portfolio`, `alpha_lab.ratings`, or
`alpha_lab.factors` — verified by
`tests/test_calibration_regression.py::test_no_scoring_module_imports_the_calibration_or_donatien_provider_code`.
Fundamental scores, Analyst Consensus, Technical Summary, AI Research
Rating, composite scoring, historical scoring, backtesting, and portfolio
construction are bit-identical whether or not a Donatien calibration exists
in the same database
(`test_historical_scoring_is_identical_with_and_without_a_donatien_calibration_row`).

Donatien has no stock-level score, no ranking influence, and no automatic
interaction with any existing AlphaLab system in Phase 1.

## 15. Also new in Phase 1: dashboard schema self-healing

Unrelated to Donatien specifically, but fixed alongside it: every Streamlit
page now calls `create_schema(engine)` on load, matching the convention
every batch script already followed. Previously, opening the dashboard
against a database created before a later model was added (any of the
`Current*` tables, or now the External Calibration tables) would crash with
`OperationalError: no such table`. This never deletes or rewrites existing
data — `create_schema` is purely additive.

## 16. AlphaLab Macro Regime (Phase 2, step 1)

Scope decision, made before implementation: built entirely from
market-observable proxy prices via the existing `YFinanceProvider`/
`IngestionService` — no new provider architecture, no FRED or other
official-economic-data integration. Every indicator is a **market-derived
proxy**, never official economic data (CPI, GDP, employment) — that
distinction is stated on the UI page itself and must never be blurred.

**Indicators** (`alpha_lab.macro.regime`, `MACRO_PROXY_TICKERS`): CBOE
Volatility Index (`^VIX`), 10Y-3M Treasury yield spread (derived from
`^TNX`/`^IRX`), US Dollar Index trend (`DX-Y.NYB`), WTI Crude Oil trend
(`CL=F`), Gold trend (`GC=F`) — 5 indicators total. Each has its own
availability status; a missing proxy reduces `coverage` and is reported as
`UNAVAILABLE` for that one indicator only — it never fabricates a value,
never forces a Neutral/0 reading, and never invalidates the other
indicators (verified by `tests/test_macro_regime.py`'s missing-indicator
tests and `tests/test_macro_service.py`'s ingestion-failure tests).

**Regime classification**: `regime`/`regime_score` are derived only from
the two indicators with a well-established, textbook risk-on/risk-off
interpretation — VIX level and the yield curve spread (a curve inversion is
the same recession signal the NY Fed's own model uses). USD/oil/gold trend
signals (price vs. trailing 50-day SMA, the same convention
`alpha_lab.research.technical` already uses) are reported as informational
context and deliberately never fold into the regime score — their
relationship to risk regime is not a single well-established direction, and
inventing one would not be a deterministic, defensible mapping.
`MacroRegime.REVIEW` (not a forced `NEUTRAL`) is used when neither VIX nor
the yield curve spread is available.

**Provider/ingestion boundary**: unlike Donatien (a single external fetch),
macro regime proxies are ingested into AlphaLab's own `Security`/`Price`
tables via the *existing* `IngestionService` — reused exactly as any other
ticker would be, not a new ingestion path. `build_macro_assessment` itself
is a pure function over already-stored price history, computed with zero
network calls, exactly mirroring `alpha_lab.research.technical`'s design. A
proxy ticker whose ingestion fails is skipped for that refresh only (its
previously-stored price history, if any, is untouched and still
contributes to coverage) — the refresh is never aborted by one ticker's
failure.

**Point-in-time correctness**: `refresh(as_of=...)` only reads `Price` rows
with `date <= as_of` when building each proxy's history, mirroring
`HistoricalScoringService`'s own PIT filtering exactly. Without this
filter, a database already holding price rows dated after `as_of` (e.g.
from a later, unrelated refresh) could leak future observations into a
supposedly historical assessment — the same class of bug
`alpha_lab.database.queries.latest_fundamentals_as_of` exists to prevent
for fundamentals. Verified by
`tests/test_macro_service.py::test_refresh_never_uses_price_rows_dated_after_as_of`.

**Methodology honesty**: the VIX/yield-curve thresholds (§ above) are a
transparent, versioned methodology (`MACRO_METHODOLOGY_VERSION`), not a
claim of empirical backtesting or statistical validation — they encode
widely-cited textbook conventions, nothing more.

**Persistence**: `current_macro_assessment` (one row per `scope`, default
`"US"`) and `macro_assessment_snapshots` (immutable, append-only,
`snapshot_id = sha256(scope, content_hash)`), mirroring the
`CurrentExternalCalibration`/`ExternalCalibrationSnapshot` pattern exactly.
The content hash deliberately excludes every `as_of` timestamp (top-level
and per-indicator) — a calendar day passing while every proxy's value stays
genuinely flat must not defeat deduplication, the same reasoning
`ResearchSnapshot._payload_hash` already applies to `generated_at`.

**Refresh/UI**: explicit only — `scripts/refresh_macro_regime.py` or the
"Refresh macro regime" button on `app/dashboard/pages/6_Macro_Regime.py`
(a dedicated page, deliberately separate from the Donatien page, to keep
"AlphaLab's own deterministic regime" and "Donatien's external regime"
visually and architecturally distinct pending §10's future comparison
layer). No page load ever fetches market data.

**Non-integration with existing scoring**: identical guarantee to
Donatien — verified by `tests/test_macro_regression.py`'s static
import-boundary check (`alpha_lab.macro` is never imported by
`research`/`screener`/`strategy`/`backtest`/`portfolio`/`ratings`/`factors`)
and a bit-identical `HistoricalScoringService` regression test.

**Not implemented in this step** (see §5): official economic data (FRED or
similar), multi-region scopes beyond the `"US"` default, market breadth
(would require the full ingested universe, not a single-ticker proxy — no
honest single-ticker substitute exists), and inflation expectations
(breakeven rates aren't reliably available via yfinance). Comparison
against Donatien's regime label is implemented as a narrow, categorical-only
layer in Phase 2B — see §17.

## 17. Donatien ↔ Market Regime Alignment (Phase 2, step 2 / "Phase 2B")

Scope decision, made before implementation: a small, focused comparison
between the two evidence layers that already exist (§16's Macro Regime and
Donatien External Calibration) — explicitly not News, AI synthesis,
conviction scoring, portfolio weighting, or backtest integration. If any of
those become a hard dependency, the correct response is to stop and expand
scope deliberately, not fold them in here.

**Absolutely no numeric score.** `alpha_lab.alignment.alignment.Alignment`
is a 4-value enum: `ALIGNED` / `CONFLICT` / `NEUTRAL` / `INSUFFICIENT_DATA`.
There is no `alignment_score`, `conviction_score`, weighted average, or
hidden percentage anywhere in this module —
`tests/test_alignment.py::test_no_alignment_score_field_exists_on_the_model`
asserts the model has no such field.

**The deterministic mapping.** Donatien exposes exactly one field with
enough structure to classify deterministically: `scenario_weights` (a
scenario-name → weight mapping). The four scenario names consistently
observed in it — `"Reacceleration"`, `"Soft Landing"`, `"Stagflation"`,
`"Deflationary Bust"` — are a standard institutional growth/inflation
quadrant framework, not an AlphaLab invention:

| Scenario | Quadrant | Donatien lean bucket |
|---|---|---|
| Reacceleration | rising growth | `CONSTRUCTIVE` |
| Soft Landing | moderating, non-recessionary growth | `CONSTRUCTIVE` |
| Stagflation | rising inflation, weakening growth | `DEFENSIVE` |
| Deflationary Bust | contracting growth and prices | `DEFENSIVE` |

`classify_donatien_lean` sums the weight in each bucket; whichever bucket
is strictly larger is the lean. A tie, or a `scenario_weights` payload
containing none of these four names (a taxonomy AlphaLab has not verified),
resolves to `MIXED`/`UNKNOWN` respectively — never guessed.

Donatien's other fields — `dominant_regime` (free-text prose, no confirmed
vocabulary), `confidence` (a word, not a number), and `defensiveness` (a
plain number with no documented scale) — are **audit-only context**. They
are retained on `AlignmentAssessment` for display and are never inputs to
the classification
(`tests/test_alignment.py::test_dominant_regime_confidence_and_defensiveness_never_affect_the_classification`
proves changing them arbitrarily while holding `scenario_weights` fixed
does not change the result).

**The alignment table**:

| Market Regime | Donatien lean | Alignment |
|---|---|---|
| `RISK_ON` | `CONSTRUCTIVE` | `ALIGNED` |
| `RISK_ON` | `DEFENSIVE` | `CONFLICT` |
| `RISK_ON` | `MIXED` | `NEUTRAL` |
| `RISK_OFF` | `DEFENSIVE` | `ALIGNED` |
| `RISK_OFF` | `CONSTRUCTIVE` | `CONFLICT` |
| `RISK_OFF` | `MIXED` | `NEUTRAL` |
| `NEUTRAL` | any | `NEUTRAL` |
| `REVIEW` | any | `INSUFFICIENT_DATA` |
| either side missing/unavailable | — | `INSUFFICIENT_DATA` |
| any | `UNKNOWN` (unrecognized taxonomy) | `INSUFFICIENT_DATA` |

`MacroRegime.REVIEW` always maps to `INSUFFICIENT_DATA` — there is no
textbook basis for a deterministic rule that would let an unassessable
market regime still produce a directional verdict.

**Auditability.** Every field needed to answer "why was this date
`ALIGNED`?" is retained directly on `AlignmentAssessment` (and therefore on
the persisted payload): `market_regime`, `market_regime_coverage`,
`market_as_of`, `donatien_lean`, `donatien_scenario_weights`, plus the
audit-only `donatien_dominant_regime`/`donatien_confidence`/
`donatien_defensiveness`/`donatien_run_date`/`donatien_source_observed_at`/
`donatien_retrieved_at` — no opaque recomputation is ever required.

**Point-in-time correctness.** `alpha_lab.alignment.service.AlignmentService.refresh`
makes no network call; it reads whichever evidence was already knowable by
the requested `as_of` and recomputes the categorical comparison. This
required adding one new, purely additive read method to each existing
service (no new persistence pattern):

- `MacroRegimeService.get_assessment_as_of(scope, as_of=...)` — the most
  recent `MacroAssessmentSnapshot` whose own `as_of` is at or before the
  requested date. Safe because `refresh()` (§16) already guarantees a
  snapshot's content used no `Price` row dated after its own `as_of`.
  Deliberately **not** additionally filtered on `created_at` (when the
  snapshot was persisted) — unlike Donatien below, there is no
  "self-reported date vs. AlphaLab's own observation time" gap to guard
  against here, because nothing is received from a third party with its
  own claimed date. A `MacroAssessmentSnapshot`'s `as_of` already *is* the
  authoritative point-in-time identity: the snapshot is computed entirely
  from AlphaLab's own Price history, itself already filtered to
  `date <= as_of` at computation time, regardless of the real wall-clock
  time the computation happened to run (which, for any historical `as_of`,
  is necessarily well after that date — that is how historical backfill
  works, not a bug). Requiring `created_at <= as_of` here would make it
  impossible to ever compute a usable historical snapshot after the fact,
  which would break historical alignment entirely. Verified by
  `tests/test_macro_service.py::test_get_assessment_as_of_selects_by_the_snapshots_own_as_of_not_by_creation_order`,
  which persists an earlier-`as_of` snapshot strictly *after* (in
  `created_at` terms) a later-`as_of` one and confirms the correct
  (earlier) snapshot is still selected.
- `ExternalCalibrationService.get_calibration_as_of(source, as_of=...)` —
  the most recent `ExternalCalibrationSnapshot` whose `retrieved_at` is at
  or before the requested date. This filters on `retrieved_at` (when
  AlphaLab actually observed the content), **never** on Donatien's own
  `source_run_date`/`macro_report_date` — mirroring
  `alpha_lab.database.queries.latest_fundamentals_as_of`'s
  `publication_date`-based filtering ("was this knowable by then," not
  "what period does it describe"). Filtering on `source_run_date` instead
  would risk look-ahead bias: a Donatien report dated before `as_of` that
  AlphaLab did not actually retrieve until after `as_of` must never be used
  for that `as_of`.

`AlignmentService.refresh` uses these two methods unconditionally, even for
`as_of=today` — there is exactly one point-in-time-safe code path for
current and historical alignment, never a separate "current" shortcut that
could drift from it. Verified by
`tests/test_alignment_service.py::test_a_later_donatien_snapshot_does_not_affect_an_earlier_alignment`,
`::test_a_later_macro_snapshot_does_not_affect_an_earlier_alignment`, and
`::test_historical_as_of_uses_the_nearest_prior_snapshot_not_the_latest_overall`.

**Degradation.** Either source unavailable, `REVIEW`, or an unrecognized
Donatien taxonomy → `INSUFFICIENT_DATA`, always — never a fabricated
`ALIGNED`/`CONFLICT`/`NEUTRAL`. There is no "failed refresh" mode in the
network sense (this module makes no network call), so the equivalent
invariant is: recomputing with partial or absent evidence never raises and
never invents a directional verdict
(`tests/test_alignment_service.py::test_missing_donatien_evidence_produces_insufficient_data_not_a_fabricated_value`,
`::test_missing_macro_evidence_produces_insufficient_data_not_a_fabricated_value`).

**Persistence**: `current_alignment_assessment` (one row per `scope`,
reusing Macro Regime's own `"US"` default) and
`alignment_assessment_snapshots` (immutable, append-only, `snapshot_id =
sha256(scope, content_hash)`), the same `Current*`/`*Snapshot` pattern as
every other evidence layer — no third persistence convention was invented.
The content hash excludes every date/freshness field (`as_of`,
`market_as_of`, `donatien_run_date`, `donatien_source_observed_at`,
`donatien_retrieved_at`) for the same reason §16's macro content hash
excludes `as_of`: a freshness marker advancing by itself, with nothing
substantive changing on either side, must not defeat deduplication.
Verified by
`tests/test_alignment_service.py::test_identical_substantive_content_creates_no_duplicate_snapshot_across_days`.

**Refresh/UI**: explicit only — `scripts/refresh_alignment.py` or the
"Recompute alignment" button on `app/dashboard/pages/7_Alignment.py`. No
page load ever fetches data or even queries the network — this module has
no provider at all. The page shows Market Regime + Donatien = Alignment
(e.g. Market Regime `RISK_OFF` / Donatien lean `DEFENSIVE` → `ALIGNED`)
with the audit fields alongside it, plus a simple Date/Alignment/Market
regime/Donatien lean history table.

**Non-integration with existing scoring**: identical guarantee to Donatien
and Macro Regime — verified by
`tests/test_alignment_regression.py::test_no_scoring_module_imports_the_alignment_code`
(static import-boundary check) and
`::test_historical_scoring_is_identical_with_and_without_an_alignment_assessment`
(bit-identical `HistoricalScoringService` regression). The reverse
direction is checked too —
`::test_macro_and_calibration_never_import_the_alignment_code` — so the
dependency strictly flows Market Regime + Donatien → Alignment → Dashboard,
never the other way.

**Not implemented in this step** (see §5): security-level
CalibrationAlignment (sector/GICS mapping) — now implemented separately in
§18 — News, the full multi-input Conviction Layer, and any ranking/backtest
integration.

## 18. CalibrationAlignment: security-level sector context (audit-only)

Scope (approved before implementation): a small, read-only DATA
CONNECTION between Donatien's tier weight lines and individual AlphaLab
securities — explicitly **not** a new categorical judgment. It never
produces an `ALIGNED`/`CONFLICT`/`NEUTRAL` verdict (that vocabulary
belongs to §17's cross-evidence-layer comparison) and never computes a
score — it reports exactly the `pct` Donatien itself published for any
tier weight line whose `gics_sector` matches the security's own sector.

**The taxonomy gap, disclosed rather than papered over.**
`alpha_lab.calibration.sector_taxonomy` exists because AlphaLab's
`Security.sector` (sourced from yfinance) follows Morningstar's 11-sector
classification, not true GICS — despite both schemes having 11 broadly
similar sectors, several names differ (e.g. Morningstar "Technology" vs.
GICS "Information Technology"; "Financial Services" vs. "Financials";
"Healthcare" vs. "Health Care"; "Consumer Cyclical"/"Consumer Defensive"
vs. "Consumer Discretionary"/"Consumer Staples"; "Basic Materials" vs.
"Materials"), and even where names align, a specific company's Morningstar
sector and true GICS sector can still diverge at the boundaries.
`MORNINGSTAR_TO_GICS_SECTOR` is a disclosed, best-effort **name**
correspondence only — never presented as verified GICS classification —
the same transparency standard already applied to §16's VIX/yield-curve
bands and §17's scenario-weight quadrant classification. Building a
genuine GICS-classification data source was explicitly scoped out (would
require a new provider/trust boundary) in favor of this narrower,
immediately shippable connection using data AlphaLab already has.

**Computation** (`alpha_lab.calibration.sector_alignment`):
`sector_tier_weights(calibration, morningstar_sector=...)` is a pure
function — given an already-fetched `DonatienCalibration` and a
Morningstar sector string, it maps to the approximate GICS sector, then
returns one `SectorTierWeight` (tier name, matched GICS sector, `pct`,
vehicle, line name) per tier weight line whose own `gics_sector` matches.
An unmapped/unrecognized sector, `None` sector, or no matching line at all
all resolve to an empty list — never a fabricated weight.
`get_sector_tier_weights_for_ticker(engine, ticker)` is the DB-backed
convenience wrapper: reads the security's stored `sector` and the current
Donatien calibration, then applies the pure function.

**No persistence, no network.** Unlike every other evidence layer in this
document, CalibrationAlignment has no `Current*`/`*Snapshot` tables at
all — it is recomputed fresh on every call from already-stored
`Security.sector` + `CurrentExternalCalibration`, exactly like a database
join. There is nothing to refresh and nothing to go stale independently of
its two already-refreshed inputs.

**UI**: a new section on `app/dashboard/pages/4_Company_Research.py`
("Donatien External Calibration — sector context") shown for whichever
ticker is already selected on that page, clearly labeled audit-only with
the Morningstar/GICS caveat stated inline. No new dedicated page — this is
per-security context, not global market context (unlike §16/§17's own
pages).

**Non-integration with existing scoring**: verified by
`tests/test_sector_alignment.py::test_no_scoring_module_imports_sector_alignment_code`
(static import-boundary check, same pattern as every other evidence
layer). `test_no_alignment_verdict_or_score_field_on_sector_tier_weight`
structurally asserts the output model carries no score/verdict/confidence
field.
