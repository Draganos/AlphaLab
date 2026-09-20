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
- **News Engine** (`alpha_lab.news`, Phase 2C) — a PIT-safe news evidence
  layer: ingestion → validated/stored article → deterministic read
  exposure only. **No sentiment, no relevance score, no NewsImpact
  classification, and no code path into any scoring or ranking
  calculation** — see §19.

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
| News Engine (`alpha_lab.news`) | `YFinanceProvider.get_news` (recent Yahoo Finance news items, per ticker) | `NewsArticle` (validated, deduplicated) | `news_articles` (append-only, no `Current*` counterpart) | `published_at`, `retrieved_at`, `created_at` | **No — evidence only, no sentiment/relevance/classification, no scoring authority** |
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

- **News Engine** (`alpha_lab.news`, §19) is now implemented, but only as
  an evidence layer: ingestion → validated/stored article →
  deterministic read exposure. The following remain **NOT IMPLEMENTED**:
  - **NewsImpact** — a future classification of news events against
    existing theses/calibration (distinct from the News Engine itself,
    which only stores and exposes articles verbatim).
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
  - **Full Conviction Layer** — a cross-domain agreement/conflict
    assessment spanning more than Market Regime + Donatien (e.g. adding
    News, Analyst Consensus, or AI Research Rating as further inputs).
    §17's Alignment is a first, deliberately narrow instance of this
    concept — not the full layer — and per the project brief must never
    become a simple average when the fuller version is built.
  - **Signal Conflict Detection** beyond §17's two-input case.
  - **NewsImpact** (see above) is itself a prerequisite input to a future
    Full Conviction Layer, not something this bullet duplicates.
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

## 19. News Engine (Phase 2C)

Scope (approved before implementation, following a dedicated sequencing
decision after §18 shipped): an evidence layer only —
`ingestion -> validated/stored article -> deterministic read exposure`.
No sentiment, no relevance score, no `NewsImpact` classification, no
conviction, and no code path into `alpha_lab.research`/`.screener`/
`.strategy`/`.backtest`/`.portfolio`/`.ratings`/`.factors`.

**Provider**: `YFinanceProvider.get_news` (`alpha_lab.providers.yfinance_provider`),
implementing the previously-unused `ResearchNewsProvider` interface,
reusing the existing `call_with_classification` error-classification
wrapper (no new provider-error taxonomy). Chosen over a paid
aggregator/API-key source or a per-company RSS registry because it
requires no new dependency (yfinance is already pinned) and is already
ticker-scoped, which also settles entity association for free (see
below).

**Schema caveat, disclosed rather than glossed over**: yfinance's news
JSON shape has changed across library versions (an older flat
`{title, link, publisher, providerPublishTime}` shape and a newer nested
`{"content": {...}}` shape), and this environment has no network access
to confirm which shape is live today. `_normalize_news_item` tries both
known shapes and returns `None` for anything matching neither — such an
item is dropped, never guessed into a fabricated record. If the live
shape turns out to be a third, unrecognized form, every item is safely
dropped (an honest empty result) rather than silently returning wrong
data.

**Coverage caveat** (mirrors §16's "market-derived proxies, never
official data" honesty pattern): `get_news` returns whatever Yahoo
currently has cached for a ticker — a handful of recent items, not a
historical archive. A refresh can only ever capture news *from the point
it is run onward*; it can never retroactively backfill news that existed
before AlphaLab first refreshed a given ticker. This must never be
presented as a source capable of validating a backtest against news from
before the feature existed.

**Ticker/entity association**: by construction, not fuzzy resolution —
`get_news(ticker)` is already scoped to one `Security.ticker` per call,
exactly like `IngestionService.ingest(ticker, ...)`. No entity-resolution
logic was introduced or is needed.

**Persistence**: `news_articles` (`alpha_lab.database.models.NewsArticleRecord`)
is append-only with **no `Current*` counterpart** — a deliberate departure
from the `Current*`/`*Snapshot` pattern used by Macro Regime/Donatien/
Alignment, because News is inherently a growing log of many observations
per ticker over time (mirroring `SECCompanyFact`'s shape), not a single
"latest state" to upsert. Deduplication is a `UniqueConstraint` on
`content_hash` (`alpha_lab.news.article.compute_content_hash`, over
`{ticker, url, title, published_at}` — deliberately excluding
`publisher`/`summary`, since a provider correcting either must not be
treated as a new article).

**Point-in-time correctness**. Three distinct timestamps, never
substituted for one another:

| Field | Meaning | PIT role |
|---|---|---|
| `published_at` | The source's own claimed publication time | Informational only — display/windowing (`since`/`until`), **never** the eligibility boundary |
| `retrieved_at` | When AlphaLab's own refresh actually observed and stored this article | **The sole PIT eligibility boundary** |
| `created_at` | Database write time | Pure bookkeeping, never queried |

`NewsService.get_history(ticker, as_of=...)` filters
`retrieved_at <= end_of(as_of)` — the identical convention already used by
`ExternalCalibrationService.get_calibration_as_of`, for the identical
reason confirmed correct in the Phase 2B PIT review: an article whose
`published_at` predates a historical `as_of` but whose `retrieved_at` is
*after* it must never leak into that historical query, because AlphaLab
genuinely did not have it stored yet. Verified by
`tests/test_news_service.py::test_as_of_excludes_an_article_retrieved_after_the_query_date`.

**Validation/failure semantics**: required fields are a non-empty title,
a well-formed `http(s)` URL, and a parseable `published_at`
(`alpha_lab.news.article.parse_news_article`) — any record missing one is
dropped individually (never fabricated), never aborting the rest of the
batch. Provider-call failures are classified via the existing
`ProviderErrorKind` taxonomy and raised before any database write
(fetch-before-write), so a failed refresh leaves every previously stored
article, for every ticker, completely untouched — verified by
`tests/test_news_service.py::test_a_later_failed_refresh_never_erases_previously_stored_articles`.

**Refresh/UI**: explicit only — `scripts/refresh_news.py <tickers...>` or
the "Refresh news for this ticker" button on
`app/dashboard/pages/4_Company_Research.py`. No page load or ticker change
ever fetches news. Placed as a section on the existing Company Research
page (per-security evidence), not a new dedicated page — mirroring §18's
placement, not §16/§17's (global market evidence).

**Non-integration with existing scoring**: identical guarantee to every
other evidence layer — verified by
`tests/test_news_regression.py::test_no_scoring_module_imports_the_news_code`
(static import-boundary check) and
`::test_historical_scoring_is_identical_with_and_without_news_articles`
(bit-identical `HistoricalScoringService` regression).

**Not implemented in this step** (see §5): `NewsImpact` (classification of
news against theses/calibration), sentiment/relevance scoring, any News
input to the Full Conviction Layer, and any ranking/backtest integration.

## 20. Donatien schema revision (Phase 2G)

Donatien's live payload changed shape between when `DonatienCalibration`
(§ above, `alpha_lab.providers.donatien`) was originally modeled and
2026-09-14, when the live payload was fetched and inspected directly for
the first time in an environment with real network access. Confirmed,
field by field, against the actual response (not the earlier hypothesis
from a validation-error message alone):

- **No longer present**: `run_time`, `confidence`, `defensiveness`,
  `top_drivers`, `key_changes`, `trend_contrarian_split`,
  `tiers.<tier>.expected_behaviour`.
- **Newly present**: top-level `macro_report` (a report filename) and
  `note` (free text); per-tier `trend_pct`/`contrarian_pct` (the same
  trend/contrarian concept `trend_contrarian_split` used to express as one
  string like `"80/20"`, now pre-split into two numbers); per-weight-line
  `tag` (`"trend"`/`"contra"` observed, no documented full vocabulary,
  modeled as free text like `confidence`/`dominant_regime`).

**Why every changed field became Optional rather than the model being
replaced**: `ExternalCalibrationService.get_current_calibration()`/
`get_history()` re-validate every previously persisted
`normalized_payload` against `DonatienCalibration` on every read, not just
at write time. A hard schema replacement would have made a row persisted
under the original shape unreadable the moment this file changed. Every
field that differs between the two observed shapes is therefore Optional
(default `None`) on both sides, so one model accepts either shape; a field
being `None` means the source did not report it under whichever shape
produced that particular observation, never inferred from the other
shape's equivalent field. `extra="forbid"` is unchanged at every level —
a field neither shape has ever reported must still fail loudly, not be
silently accepted.

`app/dashboard/pages/5_External_Calibration.py` was updated to render
every now-Optional field's absence explicitly (e.g. "Not reported by
source for this observation") instead of the unconditional access that
previously crashed (`TypeError: 'NoneType' object is not iterable` on
`top_drivers`/`key_changes`) against the live payload — reproduced before
the fix, confirmed clean after it.

## 21. Analyst estimates (Phase 2H)

`alpha_lab.database.models.Estimate` and its persistence helper
(`alpha_lab.ingestion.estimates.snapshot_estimates`, content-hash-deduped,
genuinely point-in-time) already existed but were never populated by any
provider — `analyst_revisions` category coverage and `forward_pe`/
`current_consensus_eps` were structurally always 0%/`None` for every
security, not because of a code defect but because nothing had ever
called `snapshot_estimates`.

**Provider**: `YFinanceProvider.get_estimates` (`EstimateProvider`
interface), using yfinance's `get_earnings_estimate()`/
`get_revenue_estimate()` (available since the yfinance 1.7.0 already
pinned; not previously wired up). Only the current/next **fiscal-year**
consensus (`"0y"`/`"+1y"`) is captured. yfinance also exposes current/next
**quarter** consensus (`"0q"`/`"+1q"`), but never an exact fiscal-period-
end date for them — deriving one would mean adding 3/6 months to the
company's last-reported-quarter-end, which for a company whose quarters
end on a calendar month boundary (e.g. Dec 31) while an intervening
quarter ends on a shorter month (e.g. Jun 30) can silently land a day off
the true quarter-end (confirmed against real AAL/MA data during this
investigation). Rather than persist a `fiscal_period` that could be
fabricated by a day, the quarterly periods are not captured at all.
`"0y"` uses the company's own `nextFiscalYearEnd` (from `get_info()`)
directly; `"+1y"` is exactly 12 months after it — both precise, since a
fiscal year is unambiguously 12 months and a 12-month step never crosses
into a differently-sized month regardless of the anchor day.

**Missing vs. no coverage**: an ETF's estimate call legitimately returns
an empty result (confirmed live: yfinance itself returns an empty frame
for FTEC/GDX, not an error) — this is "no analyst estimate coverage for
this instrument type," structurally identical to how
`get_analyst_consensus` already treats an ETF's missing `recommendationTrend`
row. A period whose `avg` (consensus EPS) is missing is dropped, never
zero-filled; `estimate_dispersion` is `None`, never `0`, when `low`/`high`
are unavailable. A genuine provider failure (network, rate limit) still
raises the existing classified `ProviderError` — distinct from, and never
conflated with, a real empty result.

**Refresh**: `scripts/refresh_estimates.py`, mirroring
`refresh_supplemental_research.py`'s per-ticker failure isolation — one
ticker's `ProviderError` never aborts the rest of the run and never
erases that ticker's previously stored estimates; a no-coverage ticker is
reported separately from a failure, never conflated with one.

**Measured effect** (real 5-ticker universe, NVDA/MA/AAL/FTEC/GDX):
`valuation` category coverage rose from 44% to 52% (the new `forward_pe`
metric, fed by `current_consensus_eps` via the existing, unmodified
`calculate_revision_factors`/`calculate_valuation_factors`) for the three
equities with analyst estimate coverage; FTEC/GDX unchanged (genuinely no
coverage). `analyst_revisions` category coverage remains 0% and will stay
0% until a second, later refresh exists to compare against — revision
history accumulates only from genuinely distinct future observations,
never backfilled from today's snapshot. AI Research Rating gate behavior
(pass/fail per ticker) is unchanged; only the underlying evidence-coverage
number moved up slightly for the three equities, exactly as the existing,
unmodified gates should respond to genuinely more evidence.

**Not implemented in this step**: quarterly estimates (see above, blocked
on precision, not a missing capability); `CompanyDocument` population —
investigated (yfinance's `get_sec_filings()` returns filing metadata and
links only, never the filing's own text, which `CompanyDocument.text`
requires) and explicitly not implemented, since fetching and extracting
text from each linked external filing page would be exactly the
"scraping pages for financial-looking text" this phase's own rules
forbid; estimate revision history for any of the five tickers (requires a
second refresh at a later date, not something this phase can produce by
construction).

## 22. Analyst rating changes & estimate revision trend (Phase 2I)

Two further, deliberately separate analyst-evidence layers, alongside the
two that already existed: Analyst Consensus (§ `analyst_consensus.py`,
"what analysts currently think", one upserted row per ticker) and the
pre-existing `analyst_revisions` **scoring category**
(`alpha_lab.ratings.estimates.calculate_revision_factors`, derived from
accumulated `Estimate` observations, feeding `StockResearch.overall_score`).
Neither new layer touches the scoring category, `Estimate`, or
`StockResearch.overall_score` — both are supplemental research evidence
only, exactly like Analyst Consensus / Technical Summary / AI Research
Rating. See `alpha_lab.research.analyst_events`'s module docstring for the
full four-layer map.

**Investigation** (live probe against NVDA/MA/AAL/FTEC/GDX,
`YF_DISABLE_CURL_CFFI=1`): confirmed `yf.Ticker.get_upgrades_downgrades()`
returns a ticker's ENTIRE rating-change history in one call (real
historical `GradeDate` per row — 986/515/409 rows for NVDA/MA/AAL), and
`get_eps_trend()`/`get_eps_revisions()` return the source's own already-
computed EPS-consensus trend (current/7/30/60/90-days-ago) and analyst
up/down revision counts per fiscal period from a single call — genuine
revision evidence with no multi-run accumulation delay, unlike
`calculate_revision_factors`. `TechnicalSummary.overall_rating`/
`moving_average_rating`/`oscillator_rating` were confirmed (code and UI:
`_render_technical_summary_panel`) to already exist and already render —
no technical-code changes were made this phase.

**Analyst Rating Changes**: `AnalystRatingChange` — append-only, no
`Current*` counterpart (mirrors `NewsArticleRecord`: a discrete graded
event with its own real historical date, not a periodic snapshot).
`YFinanceProvider.get_analyst_rating_changes` (new `AnalystEventProvider`
interface); `alpha_lab.ingestion.analyst_events.snapshot_analyst_rating_changes`
content-hash-dedupes, so re-persisting the same full history on every
refresh (the source always returns everything, not deltas) is idempotent.
A price target of exactly `0` (yfinance's placeholder when an initiation
has no prior target) is normalized to `None` — never presented as a real
$0 target.

**Estimate Revision Trend**: `EstimateRevisionTrend` — a point-in-time
snapshot of the source's own trend/revision-count reading, keyed by the
same precise annual-only (`"0y"`/`"+1y"`) `fiscal_period` anchor as
`Estimate` (the identical quarterly-date-precision exclusion from Phase
2H applies here — the same `_fiscal_anchors`/`_fiscal_period_for` helpers
are reused). `alpha_lab.ingestion.estimate_revisions.snapshot_estimate_revisions`
content-hash-dedupes per observation.

**Read/orchestration**: `alpha_lab.research.analyst_events.AnalystEventsService`
— pure DB reads (`get_rating_changes`, `get_latest_revision_trend`, the
latter returning only the newest observation per fiscal period, never a
mix of stale and current) plus explicit refresh methods; `refresh_all`
attempts both layers independently so one domain's `ProviderError` never
blocks or is masked by the other.

**UI**: Company Research page, new "Analyst Rating Changes & Estimate
Revision Trend" section between the existing supplemental-research refresh
button and the snapshot-save section — two tables plus one refresh button,
explicitly captioned as non-scoring evidence distinct from both Analyst
Consensus above and the Analyst Revisions category further below.

**AI Research Rating integration — deliberately not wired in this phase**:
`AIEvidenceCoverage.analyst_coverage` is a fixed formula over
`AnalystConsensus`'s nine documented fields, and
`DeterministicAIRatingProvider`'s dimension-banding logic
(`_dimension_value_for_evidence`) only knows how to band a single scalar
value per evidence item. Turning multi-row rating-change/revision-trend
data into one scalar (e.g. "net upgrades in the last 90 days") is a real
methodology decision with genuine interpretive judgment calls, not a
natural fit — left for explicit future approval rather than forced in,
mirroring this phase's Part E precedent of stating "not needed" rather
than changing code that doesn't need it.

**Measured effect** (real 5-ticker universe, NVDA/MA/AAL/FTEC/GDX,
verified via direct DB query, not just script output): `AnalystRatingChange`
rows went from 0 to 1,910 (NVDA 986, MA 515, AAL 409; FTEC/GDX genuinely
0 — no analyst coverage, confirmed via a handled 404, not a failure).
`EstimateRevisionTrend` rows went from 0 to 6 (NVDA/MA/AAL × 2 fiscal
periods each; FTEC/GDX genuinely 0). Both refreshes are confirmed
idempotent: an immediate re-run against the same real data stored 0 new
rows for every ticker. Neither change altered any scoring output — the
three established smoke tests (`scripts/smoke_test.py`,
`scripts/smoke_test_phase2.py`, `scripts/smoke_test_phase3.py`) produce
identical scores/ratings/coverage to their pre-Phase-2I values, and
`git diff` on every scoring-path file (`alpha_lab/screener/service.py`,
`alpha_lab/ratings/estimates.py`, `alpha_lab/strategy/`) is empty.

## 23. Canonical Analyst Research summary + AI integration (PR #26)

Turns the two Phase 2I evidence tables into a fourth canonical research
field and, for the first time, lets the AI Research Rating consume analyst
event/revision evidence -- while keeping every existing gate, coverage
formula, and scoring category exactly as it was.

**`alpha_lab.research.analyst_research`** (new, pure/deterministic module):
`AnalystResearchSummary` composes `AnalystRatingChange` +
`EstimateRevisionTrend` rows (already-fetched ORM rows in, no provider
call, no DB access) into: the 10 most recent rating-change events, a
trailing-90-day upgrade/downgrade/initiation/reiteration tally (`None`,
never a fabricated zero, when the ticker has no rating-change history at
all -- a real `{"upgrades": 0, ...}` once history exists but nothing
happened in the window), and a per-fiscal-period revision trend with an
honest `RevisionDirection` (`IMPROVING`/`DETERIORATING`/`STABLE`/`REVIEW`
-- `REVIEW`, never a guess, when the current or 30-day-ago EPS trend value
is missing). `coverage` is domain-aware across the two sub-domains present
(rating changes, revision trend), mirroring `AIEvidenceCoverage`'s
never-excluded-from-the-denominator pattern. Exposed as
`StockResearch.analyst_research` (via `AnalystEventsService.
get_research_summary`, wired into `ResearchService.get_stock_research`
exactly like `analyst_consensus`/`technical_summary`/
`ai_research_assessment` before it) -- same "`None` means not computed for
this research state" convention, verified to never leak into an
already-persisted historical snapshot.

**Company Research UI**: the rating-changes/revision-trend tables now read
from `research.analyst_research` (the canonical field) instead of calling
`AnalystEventsService` directly for display -- aligning with how the
Analyst Consensus/Technical Summary panels already work. The refresh
button still calls `AnalystEventsService.refresh_all` directly (writes go
through the service; only reads go through the canonical model). Added a
90-day tally caption and a "Direction" column.

**AI Research Rating integration** (`AI_RATING_METHODOLOGY_VERSION` bumped
`v2` -> `v3`; old persisted assessments keep interpreting under their own
version): `build_evidence_payload` gained an `analyst_research` parameter
producing two new evidence items, each omitted entirely (never banded to
NEUTRAL) when the underlying data was insufficient to compute it --
`analyst_events:net_rating_changes_90d` (only when
`rating_change_counts_90d` is not `None`) feeds `business_outlook`
alongside the existing `analyst:rating`, and `estimate_revision:
trend_direction` (only when the nearest period's direction isn't
`REVIEW`) feeds `growth_prospects` alongside `fundamental:earnings_growth`.
Both are genuine, deterministic derived facts (a real event count, a real
EPS-trend comparison) -- never fabricated, never inferred beyond what the
stored evidence actually shows. Explicitly NOT changed: `AIEvidenceCoverage`
(still fundamental/analyst/technical only), `AI_MINIMUM_ASSESSABLE_
DIMENSIONS`/`AI_MINIMUM_EVIDENCE_COVERAGE`, and every other AI gate --
this is new evidence citable within the existing gate, not a weaker gate.
`SupplementalResearchService.refresh_all` supplies `analyst_research` via
a pure DB read (`AnalystEventsService.get_research_summary`) -- refreshing
that evidence from a provider remains its own separate explicit action,
never triggered implicitly by an AI refresh.

**Measured effect** (real 5-ticker universe, verified via direct
`ResearchService`/`SupplementalResearchService` calls, not just script
output): NVDA and MA's revision trend read `IMPROVING` (real EPS-estimate
increases over the trailing 30 days); AAL's reads `DETERIORATING` (real
EPS-estimate decline) -- refreshing the AI Research Rating for AAL with
this evidence moved its rating from what fundamentals/consensus alone
would suggest to `NEUTRAL`, an honest reflection of genuinely softening
analyst sentiment, not a defect. FTEC/GDX's `analyst_research` remains
`None` (confirmed genuine no-coverage, unchanged from Phase 2I). All three
equities' AI assessments now genuinely cite both new evidence IDs. The
three established smoke tests and the full test suite remain unchanged in
outcome; every scoring-path file diff is empty.

**Not implemented in this step**: no change to `calculate_revision_factors`,
`Estimate`, or the `analyst_revisions` scoring category -- those remain
exactly as Phase 2H/2I left them, and (confirmed via a real re-refresh 3
days after the first) still correctly read 0% coverage today because the
7/30/90-day revision windows need more elapsed time between AlphaLab
refreshes than has passed so far, not because of any defect.

## 24. Technical indicator agreement/disagreement (PR #27)

Technical coverage was already ~100% on the real 5-ticker universe before
this PR, and `TechnicalSummary.overall_rating`/`moving_average_rating`/
`oscillator_rating` already existed, coverage-gated and REVIEW-honest, in
both code and UI (confirmed in Phase 2I's Part E audit) -- so this PR adds
no new indicators and does not re-slice the existing moving-average/
oscillator grouping into new trend/momentum/volatility buckets, per the
roadmap's own "do not add indicators simply to increase a coverage
percentage" and "if a technical interpretation already exists and is
adequate, preserve it" rules.

**The one genuine gap**: `overall_rating` reflects the *average* direction
of available indicators, not how *unanimous* they are. Two tickers can
share the same rating while one is near-unanimous and the other is a
coin flip that happened to average to the same band -- `overall_rating`
alone hides that difference, and nothing surfaced it: a viewer had to
manually read the per-indicator expander and count Buy/Sell/Neutral
themselves.

**`IndicatorAgreement`** (new enum: `STRONG_AGREEMENT`/`MODERATE_AGREEMENT`/
`MIXED`/`REVIEW`) plus `buy_signal_count`/`sell_signal_count`/
`neutral_signal_count` on `TechnicalSummary` -- computed in
`build_technical_summary` from the exact same 15 indicator signals already
computed, via a `Counter` over `ma_signals + osc_signals`. Purely additive
evidence: `overall_score`/`overall_rating`/`moving_average_*`/
`oscillator_*` are computed identically to before, byte-for-byte (verified
by the existing, unmodified test suite passing unchanged). The new fields
are `Optional` (default `None`) specifically so a `TechnicalSummary`
already persisted before this PR (in `CurrentTechnicalSummary.payload` or
embedded in a historical `ResearchSnapshot.payload`) still re-validates on
read -- mirrors the exact Optional-field backward-compatibility pattern
established for Donatien's schema revision (§20).

**Bug caught before merge**: the agreement threshold used `>=`, so an
exact tie between exactly two non-zero categories (e.g. 5 Buy / 5 Sell / 0
Neutral -- the single clearest possible disagreement) always lands
`dominant_fraction` on precisely 0.5, which incorrectly cleared the
`MODERATE_AGREEMENT` threshold. Fixed to strict `>`; a regression test
(`test_exact_even_buy_sell_split_is_mixed_not_moderate_agreement`) proves
a 5/5/0 and 6/6/0 split both read `MIXED`.

**UI**: Company Research's Technical Summary panel gained one caption line
-- "Indicator agreement: <label> — N Buy · N Sell · N Neutral (of N
available)" -- directly below the existing coverage/timeframe/as-of line.

**Measured effect** (real 5-ticker universe, via
`SupplementalResearchService.refresh_technical_summary`): NVDA (5/5/5
split) and MA (7/6/2) both carry a `NEUTRAL` overall rating that turns out
to be genuine `MIXED` disagreement, not a mild-but-unanimous reading --
exactly the distinction this evidence exists to surface. AAL and FTEC show
`MODERATE_AGREEMENT` behind their `STRONG_SELL`/`BUY` ratings respectively
(a real majority, not unanimous). Full test suite and all three
established smoke tests pass with scores identical to before this change;
every scoring-path file diff is empty.

## 25. Research Evidence & Coverage Dashboard (PR #28)

By PR #27, AlphaLab had eight fundamental-category coverage numbers plus
four independently-computed evidence-domain coverage numbers (Analyst
Consensus, Analyst Research's two sub-domains, Technical Summary, AI
Research Rating) and two more outside `StockResearch` entirely (News,
Macro Regime) -- but nothing brought them into one place. Answering "how
much evidence do I actually have for NVDA" meant opening Company Research
and reading five separate panels by eye; answering it for the whole
universe meant nothing at all.

**`alpha_lab.evidence_coverage`** (new top-level package, deliberately
*not* under `alpha_lab.research`): a pure read-model layer combining every
domain above into one `SecurityCoverageSummary` (a `CoverageRow` per
category: `coverage`, `status`, `evidence_count`, `freshness`, `providers`,
`limitation_reason`) plus `flatten_coverage_rows` for universe-wide
breakdowns. It computes nothing new -- every figure is read verbatim off a
value that already exists somewhere else in the codebase (`category.
coverage`, `analyst_consensus.total_analysts`, `technical_summary.
moving_average_available + oscillator_available`, `ai_research_assessment.
evidence_coverage.overall_ai_evidence_coverage`, ...). No second
composite/overall score is produced anywhere in this module (see
`test_never_produces_a_second_overall_or_composite_score`).

**Why its own top-level package, not `alpha_lab.research`**: `alpha_lab.
news`/`alpha_lab.macro`'s own module docstrings declare that nothing in
`alpha_lab.research`/`.screener`/`.strategy`/`.backtest`/`.portfolio`/
`.ratings`/`.factors` may import them -- `tests/test_news_regression.py`/
`test_macro_regression.py` enforce this by scanning those directories'
source text for the substrings `alpha_lab.news`/`alpha_lab.macro`, which
catches a docstring mention as readily as a real import. A layer that
legitimately needs both `StockResearch` and News/Macro evidence has to sit
outside that boundary, exactly like `alpha_lab.alignment` (Donatien <->
Macro Regime) already does -- `alpha_lab.evidence_coverage` follows the
same precedent and imports `alpha_lab.research`/`.news`/`.macro` types
directly, in the one direction the guard rails allow.

**Four honest `CoverageStatus` values**, no finer than the underlying data
supports: `FULL` (coverage 1.0), `PARTIAL` (0 < coverage < 1.0),
`NO_EVIDENCE` (computed, coverage 0), `NOT_COMPUTED` (the underlying
object is `None` -- never computed for this research state at all, kept
distinct from a confirmed zero). `limitation_reason` is populated only
where the underlying model already distinguishes a cause (e.g. "below the
50% minimum indicator coverage gate", "only 1/3 required dimensions
assessable", "no analyst coverage confirmed for this security") --
per-metric "insufficient history" vs "provider failure" vs "confirmed
unavailable" is deliberately **not** invented here, since `alpha_lab.
research.model`'s own docstring already documents that `MetricStatus.
INVALID` is reserved but unwired and today's coercion helpers collapse
every non-finite/missing fundamental input to the same `None` -- this
module only ever reports what is already knowable upstream.

**Analyst History / Revisions** are split out of the single blended
`AnalystResearchSummary.coverage` (0/0.5/1.0 across the two sub-domains)
into two separate rows, each independently `NOT_COMPUTED` (object is
`None`) / `NO_EVIDENCE` (0.0, that sub-domain has no rows) / `FULL` (1.0,
that sub-domain has rows) -- a coarser binary reading than the fundamental
categories' fractional coverage, since that is genuinely all the
underlying data supports; no fractional number is fabricated to look more
precise.

**News** has no pre-existing coverage baseline at all (`alpha_lab.news.
service`'s own docstring: a refresh only ever captures news from the point
it is run onward, never a historical archive), so its row uses
presence-based coverage (1.0 if any article was retrieved, 0.0 if queried
and confirmed empty) rather than a fabricated fractional percentage --
`news_articles=None` (not queried) and `news_articles=[]` (queried, zero
found) are kept distinct (`NOT_COMPUTED` vs `NO_EVIDENCE`).

**Macro Regime** is market-wide, not security-specific (`alpha_lab.macro.
regime`'s own docstring), so its row reads identically for every security
evaluated at the same time; the row is explicitly labeled "Macro Regime
(market-wide)" and its `limitation_reason` says so, so a universe-wide
breakdown by security never implies a per-security macro reading that
does not exist.

**UI**: new `app/dashboard/pages/8_Evidence_Coverage.py`, two tabs.
"Security Detail" renders one security's full `CoverageRow` table plus a
weak/missing-category detail list. "Universe Breakdown" groups
`flatten_coverage_rows` (one flat dict per security x category x provider,
so a provider-level breakdown counts each contributing provider once
rather than stringifying a list) by Category/Security/Security type/
Sector/Provider via `pandas.groupby`, reporting `avg_coverage` plus
full/no-evidence/not-computed counts per group. Opening the page performs
no provider call and no scoring -- pure reads through the existing
`ResearchService`/`NewsService`/`MacroRegimeService` read paths. The
Universe Breakdown tab's initial fetch (`get_stock_research` +
`NewsService.get_history` per security) is wrapped in `st.cache_data`
(underscore-prefixed non-hashable arguments, TTL 300s) so switching the
"Breakdown by" selector -- a pure client-side regroup of already-fetched
rows -- never re-triggers the full 2N-read fetch.

**Bug caught before opening the PR** (self-review): `_ai_evidence_row`
could attach a gate/dimension-shortfall `limitation_reason` to a row whose
`status` was already `FULL`, because `overall_ai_evidence_coverage` (a
separate average of raw fundamental/analyst/technical coverage) and "how
many of the 6 AI dimensions were assessable" are independent numbers -- a
security can reach `coverage == 1.0` while the provider still rated only a
minority of dimensions. Fixed by skipping the reason entirely once
`status is CoverageStatus.FULL`, mirroring every other row builder in this
module. Regression test:
`test_ai_evidence_row_never_carries_a_reason_when_status_is_full`.

**Bug caught in a second bug-check pass after opening the PR** (self-review):
the Universe Breakdown tab's `pandas.groupby` ran directly on
`flatten_coverage_rows`' output, which deliberately explodes one
`CoverageRow` into one flat dict per provider it cites. Grouping that
exploded data by anything other than `provider` silently double-counted
any (security, category) pair citing more than one provider -- e.g. a
category sourced from two providers would be weighted 2x in every
Category/Security/Security type/Sector breakdown's `avg_coverage` and
status counts, none of which is true of a `provider`-grouped view (each
contributing provider is correctly counted there). Extracted the grouping
itself out of the Streamlit page into a new pure, tested function,
`summarize_universe_breakdown(flat_rows, group_by)`, which de-duplicates
on (ticker, category) before grouping by anything other than `provider`.
Regression tests:
`test_summarize_universe_breakdown_does_not_double_count_multi_provider_categories`,
`test_summarize_universe_breakdown_by_provider_counts_each_provider_once`.
Harmless on the current real dataset (no category in it cites more than
one provider today), confirmed by an unchanged Universe Breakdown table
before/after the fix in the live UI -- but wrong as soon as a category
gains a second provider.

**Bugs caught in a third bug-check pass** (self-review): `_ai_evidence_row`
and `_technical_row` both left `limitation_reason` as `None` for a
`PARTIAL` row that already cleared every applicable gate/threshold --
every other row builder in this module explains any non-`FULL` status,
but these two only explained the below-gate case. Confirmed actually
happening on the real 5-ticker universe: NVDA/MA/AAL's AI Evidence rows
(87-92% coverage, necessarily `PARTIAL`) rendered "no further detail
available" in the UI despite being visibly not fully covered. Fixed by
adding a generic `"{available}/{total} ... assessable/available"` reason
for that PARTIAL-but-above-gate case in both row builders, matching the
style already used by the fundamental-category and Analyst Consensus
rows. Regression tests:
`test_technical_row_still_explains_partial_coverage_above_the_gate`,
`test_ai_evidence_row_still_explains_partial_coverage_that_clears_both_gates`.
A third, more minor finding (`summarize_universe_breakdown`'s
`na_position="first"` sort intentionally differs from the pre-refactor
inline groupby's pandas default) was confirmed correct-as-is -- it matches
the Security Detail tab's existing "`NOT_COMPUTED` sorts first" convention
-- and only needed a docstring note plus a regression test
(`test_summarize_universe_breakdown_sorts_all_not_computed_groups_first`),
no behavior change.

**Real-data validation** (real 5-ticker universe plus the full persisted
universe including macro-proxy instruments): NVDA/MA/AAL correctly show
`FULL` Analyst Consensus/History/Revisions; FTEC/GDX correctly show
`NO_EVIDENCE` for Analyst Consensus ("no analyst coverage confirmed for
this security") and `NOT_COMPUTED` for Analyst History/Revisions (no
`AnalystResearchSummary` at all), matching the project's already-verified
"FTEC and GDX legitimately lack conventional equity analyst coverage"
fact. AI Evidence coverage is visibly lower for the two ETFs (38%, "only
1/3 required dimensions assessable") than for the three equities
(87-92%), reflecting genuinely thinner upstream evidence, not a
fabricated penalty. A universe-wide Provider breakdown correctly separates
`YFinanceProvider`/`AlphaLabPriceHistory`/`deterministic-rule-based`
contributions. Verified live in the Streamlit UI (both tabs, all five
breakdown dimensions) via a headless browser, not just by script.

Full test suite (`tests/test_coverage_summary.py`, 22 new tests) and all
three established smoke tests pass; every scoring-path file diff is empty
-- this PR adds a new read-only package and one new dashboard page, and
touches no file under `alpha_lab.research`/`.screener`/`.strategy`/
`.backtest`/`.portfolio`/`.ratings`/`.factors`.

## 26. Security-Type Capability Model (PR #29)

AlphaLab's eight fundamental scoring categories were designed for an
operating company with its own income statement and balance sheet. Before
this PR, every category was treated as equally applicable to every
security, so an ETF's `business_quality`/`earnings_growth`/
`financial_strength`/`valuation`/`analyst_revisions`/`shareholder_return`
categories all read `UNAVAILABLE` -- indistinguishable from an equity
whose fundamentals fetch genuinely failed. Confirmed on the real universe
before this PR: FTEC/GDX's `overall_live_coverage` was 15% and confidence
2.8/10, diluted by six categories that were never expected to have any
evidence, not six genuine gaps.

**`alpha_lab.research.security_type`** (new, pure, zero dependencies):
`SecurityType` (`EQUITY`/`ETF`/`OTHER`, extensible), `normalize_security_
type` (maps the yfinance-sourced `asset_type`/`quoteType` string,
case-insensitively; anything unrecognized -- indices, futures,
currencies, mutual funds, a future type nobody has reviewed yet -- maps
to `OTHER`), and `NOT_APPLICABLE_CATEGORIES`, which excludes exactly six
categories for `ETF` (the ones that need a company's own income
statement, balance sheet, EPS estimates, or share buybacks -- see the
module's docstring for the category-by-category rationale) and nothing
for `EQUITY`/`OTHER`. `momentum` (pure price history) and `ai_research`
(independently gated by document attributability) stay applicable to
ETFs.

**Wiring, in three places, each the smallest change that could carry the
distinction through to where it's read:**

1. `alpha_lab.research.model.CategoryStatus`/`MetricStatus` gain
   `NOT_APPLICABLE` (the latter was already reserved for exactly this,
   unused, since Phase 3). `alpha_lab.research.build._build_category` sets
   it for a category/metric that's both genuinely empty (`coverage <= 0`)
   *and* classified not-applicable for the security's type -- real
   evidence that happens to exist despite the classification is never
   suppressed (see `test_genuine_evidence_in_a_not_applicable_category_is_
   never_suppressed`).
2. `_confidence_factors`' `category_breadth` (20% of `confidence`) now
   averages only over categories whose status isn't `NOT_APPLICABLE`, so
   it no longer divides by 8 when only 2 categories could ever apply.
3. `alpha_lab.screener.service._applicable_rating_weights` filters
   `rating_weights` to applicable categories and renormalizes to sum to
   1.0 before `calculate_coverage` runs, so `overall_live_coverage`/
   `quantitative_coverage` (50%/other of `confidence`) reflect coverage of
   what's applicable, never diluted by what isn't. For `EQUITY` this
   returns the identical `rating_weights` object, not just an equal one.

**Deliberately NOT touched**: `alpha_lab.ratings.coverage.calculate_
coverage` itself (unchanged signature and logic -- it already iterates
only over whatever `weights` dict it's given), and `_category_score`/
`overall_score` (already excluded `None`-score categories from its own
weighted average via `available_weight`, so an ETF's `overall_score` was
never diluted by inapplicable categories in the first place -- this PR
touches only the separate coverage/confidence honesty metrics).
Historical scoring/backtesting (`alpha_lab.strategy.historical`,
`alpha_lab.factors`) shares no code with the live screener path touched
here, confirmed by grep -- untouched, as required.

**`alpha_lab.evidence_coverage`** (PR #28's dashboard) gained matching
`CoverageStatus.NOT_APPLICABLE` handling: `_fundamental_row` reports it
distinctly (never as `NO_EVIDENCE`), and `summarize_universe_breakdown`
masks `NOT_APPLICABLE` rows out of `avg_coverage` the same way -- a
`NOT_APPLICABLE` row's `coverage` is a real `0.0`, unlike `NOT_COMPUTED`'s
`None`/NaN, so it needed its own exclusion. A group whose every row is
`NOT_APPLICABLE` (e.g. "Valuation" grouped over an all-ETF universe) sorts
last (with `FULL`), not first like a genuinely empty group.

**Bugs caught in self-review before opening the PR**: (1)
`_applicable_rating_weights` fell back to the *unfiltered* weights
whenever the applicable remainder summed to zero weight, silently
reintroducing the excluded categories in that edge case -- fixed to
return the correctly-excluded-but-unrenormalized dict instead, only
falling back when literally nothing is applicable. (2)
`summarize_universe_breakdown` treated an all-`NOT_APPLICABLE` group's
`NaN` `avg_coverage` identically to an all-`NOT_COMPUTED` group's,
sorting both first -- fixed with an explicit `all_not_applicable` check
that pushes the former to sort last instead. Regression tests:
`test_applicable_rating_weights_still_excludes_when_remainder_is_all_
zero_weighted`, `test_summarize_universe_breakdown_sorts_all_not_
applicable_groups_last_not_first`.

**Real-data validation** (rebuilt current research for the live 11-
security universe, `scripts/rebuild_research.py`, DB backed up first):
NVDA/MA/AAL's `overall_score`, `overall_live_coverage`, `confidence`, and
every category's `coverage`/`status` are byte-identical before and after
this PR. FTEC/GDX: `overall_live_coverage` 15% -> 60%, confidence 2.8/10
-> 4.2/10, and `business_quality`/`earnings_growth`/`financial_strength`/
`valuation`/`analyst_revisions`/`shareholder_return` all read
`NOT_APPLICABLE` (never `NO_EVIDENCE`); `momentum` stays `PARTIAL` (real
evidence) and `ai_research` stays `UNAVAILABLE` (genuinely no attributable
documents, not reclassified). Verified live in both Company Research and
Evidence Coverage dashboards via a headless browser -- no page errors, the
`NOT_APPLICABLE` legend/rows render distinctly from `UNAVAILABLE`, and the
Universe Breakdown's new `not_applicable` column and corrected
`avg_coverage` both show correctly for a mixed equity/ETF universe.

Full test suite (`tests/test_stock_research_model.py` +6,
`tests/test_security_type.py` new (7 tests), `tests/test_live_screener_
safety_phase3.py` +6, `tests/test_coverage_summary.py` +3) and all three
established smoke tests pass.

**Two more bugs caught in a follow-up bug-check pass, both surfaced by
manual testing of the Evidence Coverage dashboard after the PR was open:**

1. `_build_category`'s `unavailable_metrics` list (meaning "expected but
   missing") was populated for every metric with no value regardless of
   `applicable`, so a `NOT_APPLICABLE` metric still appeared in it.
   Company Research's "Unavailable — no evidence available: ..." caption
   then rendered a structurally-inapplicable metric as a genuine gap,
   directly under a status line that already said `NOT_APPLICABLE` for the
   category. Fixed by gating the append on `applicable`. Regression test:
   `test_not_applicable_metrics_are_never_listed_as_unavailable_metrics`.
2. **Performance**: the Evidence Coverage page's Universe Breakdown tab
   was slow to load. Root cause: `ResearchService.get_stock_research(
   ticker)`'s own `_find_record` re-reads and re-deserializes *every*
   persisted `LiveResearchRecord` (`list_current_research()` ->
   `Phase3Repository.latest_current_payloads()`) just to find the one
   matching ticker -- calling it once per ticker in a loop, as the
   Universe Breakdown's cached loader did, costs O(n^2) database reads and
   Pydantic re-validations in universe size, not O(n). Fixed by extracting
   `get_stock_research`'s enrichment step into a new public method,
   `build_research_for_record(record)`, and having the Universe Breakdown
   loader call `list_current_research()` once and pass each already-
   fetched record to it, instead of calling `get_stock_research(ticker)`
   per ticker. `get_stock_research` itself is unchanged in behavior (now
   delegates to the new method) and its docstring documents the O(n^2)
   trap for any future caller that loops it. Measured on the real 11-
   security universe: ~40% faster already, and the win grows with universe
   size since the old path was quadratic. Output verified byte-identical
   before/after. Regression test:
   `test_build_research_for_record_matches_get_stock_research`.

**A third bug-check pass (requested after PR #29 had already merged, folded
into PR #30 since PR #29 itself cannot be reopened) found two more
low-severity issues, both text/documentation-only -- no crash, no data
corruption, no coverage-number change:**

1. `_build_category`'s `unavailable` list is gated on `applicable`, so a
   category classified `NOT_APPLICABLE` for this security type whose
   `status` nonetheless reads `PARTIAL` (real evidence leaking through
   despite the classification -- see finding 1 above; confirmed today to
   never actually happen for any of the six ETF-excluded categories, whose
   underlying metrics always fetch as uniformly empty, not partial, but
   not something the code itself rules out) would report an empty
   `unavailable_metrics` list. `_fundamental_row` then rendered "0
   metric(s) unavailable" next to non-full coverage -- self-contradictory.
   Fixed with an explicit branch: an empty `unavailable_metrics` at
   `PARTIAL` now reports "remaining metrics not applicable to this
   security type" instead. Regression test: `test_partial_not_applicable_
   category_reports_a_non_contradictory_reason`.
2. `_confidence_factors`'s docstring still described `category_breadth` as
   "the mean of the eight categories'" coverage, unchanged since before
   this PR's own `applicable_categories` filtering -- for an ETF the
   denominator is 2 (momentum + ai_research), not 8. Fixed to describe the
   actual (PR #29) behavior.

## 27. ETF Research Depth (PR #30)

PR #29 stopped FTEC/GDX's six inapplicable fundamental categories from
dragging down their coverage and confidence, but it added no genuine
ETF-specific evidence -- an ETF's actual research surface (holdings,
concentration, sector exposure, expense ratio, AUM, fund-level valuation
averages) was still entirely unfetched. This PR implements it, per the
roadmap's PR #30 spec, using exactly the fields confirmed live to be
reliably populated for the installed yfinance version (1.7.0) -- nothing
scraped just to inflate coverage.

**Provider layer** (`alpha_lab.providers.yfinance_provider.get_fund_data`):
`Ticker.funds_data` is a *lazy* scraper -- accessing the property itself
never raises; the real fetch (and a `YFDataException`, e.g. "NVDA: No Fund
data found.", for a non-fund ticker) only fires on the first sub-property
access (`.fund_overview`, `.asset_classes`, `.top_holdings`, ...). The
entire extraction -- every sub-property read -- is wrapped in one
`call_with_classification` call so a mid-extraction `YFDataException`
still gets classified and returns `None`, rather than raising unclassified
partway through. `YFDataException` (not a subclass of
`YFTickerMissingError` -- the ticker is fine, the fund *category* of data
is what's missing) is a new classification branch in
`alpha_lab.providers.errors.classify_yfinance_error`, mapped to
`ProviderErrorKind.NO_DATA` alongside the existing `YFTickerMissingError`
branch.

Deliberately excluded, confirmed unreliable by live probing during Part A
and documented in `get_fund_data`'s own docstring: `bond_holdings`/
`bond_ratings` (inconsistent shape across funds), `equity_holdings`'s
Median Market Cap and 3 Year Earnings Growth rows (`<NA>` for every fund
checked), and the `info` dict's dividend yield/YTD return fields
(inconsistent presence/units versus the dedicated `funds_data` rows).
`_fund_table_value` reads the `fund_operations`/`equity_holdings`
DataFrames *positionally* by column index (fund's own column vs. "Category
Average"), avoiding brittleness from share-class-suffix or casing
mismatches in a ticker-name lookup.

**`alpha_lab.research.fund_evidence`** (new, pure, zero `alpha_lab`
scoring dependencies): `FundEvidence` carries five independently-present
domains -- asset allocation, sector weightings, fund operations (expense
ratio/category average/turnover/AUM), equity-holdings valuation averages
(P/E, P/B, P/S, P/CF only), and top holdings -- plus a domain-aware
`coverage` (mirrors `AnalystResearchSummary`'s "absent domain counts as 0,
never excluded from the denominator" convention) and a `top_holdings_
concentration` (sum of top-holdings' weights). `build_fund_evidence`
returns `None` (never an empty-but-present object) when every domain is
absent, matching the existing "`None` means not computed" convention. A
new `CurrentFundEvidence` table persists it, mirroring
`CurrentAnalystConsensus` exactly (ticker PK/FK, JSON payload,
`computed_at`).

**Wiring**: `SupplementalRefreshService.refresh_fund_evidence` /
`get_fund_evidence` follow the same shape as the existing analyst-
consensus and technical-summary refresh/read pair; `refresh_all` always
attempts it (independent of analyst success, like technical_summary),
falling back to the last stored value on a `ProviderError`.
`StockResearch` gained a `fund_evidence` field, populated in both
`get_stock_research` and `build_research_for_record` (PR #29's O(n^2)
fix) so the Universe Breakdown path stays O(n).

**AI Research Rating** (`alpha_lab.research.ai_rating`): two changes.
(1) `build_evidence_payload` now also skips `NOT_APPLICABLE` categories
(previously only `UNAVAILABLE` was skipped), so a fund's six inapplicable
fundamental categories no longer feed the AI a fabricated "score =
unavailable" evidence item for something it was never expected to have.
(2) When `fund_evidence` is supplied, up to four new evidence items are
emitted (`fund:identity`, `fund:expense_ratio_vs_category`, `fund:
top_holdings_concentration`, `fund:sector_concentration`). Only the
expense-ratio-vs-category signal is banded into a dimension
(`valuation_context`, via new `_EXPENSE_RATIO_THRESHOLDS_V1` mirroring the
existing `_UPSIDE_THRESHOLDS_V1` shape) -- concentration and sector
exposure are deliberately left as descriptive-only evidence for a future
live LLM provider, not force-mapped to a good/bad judgment the
deterministic provider has no principled threshold for.
`build_evidence_coverage` is now security-type-aware: for `SecurityType.
ETF`, `fund_evidence.coverage` (or `0.0` if fund evidence is genuinely
absent -- never excluded from the denominator) substitutes for `analyst_
coverage` in the 3-domain average, per the roadmap's explicit "do not
reuse equity analyst gates blindly." The `EQUITY` path (the default, for
any other/omitted security type) is byte-identical to before this PR --
confirmed by a dedicated regression test.
`alpha_lab.evidence_coverage.summary` gained a matching `_fund_evidence_
row` in the per-security coverage breakdown.

**Bugs caught in a final high-effort self-review pass, before opening the
PR:**

1. `_fund_evidence_row` returned `CoverageStatus.NOT_COMPUTED` for *every*
   equity (since `fund_evidence` is always `None` for a non-fund), which
   meant every equity's Evidence Coverage Security Detail page counted
   "Fund Evidence" into `tracked`/`weak_rows` and listed it under "Weak or
   missing category detail" -- contradicting both the function's own
   intent and the `_fundamental_row` precedent from PR #29 (structurally
   inapplicable is `NOT_APPLICABLE`, never a coverage gap). Fixed by
   adding a `security_type` parameter with an early-return branch:
   `fund_evidence is None and security_type != SecurityType.ETF` reports
   `NOT_APPLICABLE`; a genuine ETF with no fund evidence *yet* still
   correctly reports `NOT_COMPUTED`. Regression tests: `test_fund_
   evidence_row_is_not_applicable_for_an_equity`, `test_fund_evidence_
   row_is_not_computed_for_an_etf_not_yet_refreshed`.
2. `top_holdings_concentration` summed only the top holdings with a
   non-`None` weight, silently understating concentration with no signal
   that it had done so if any single holding's weight came back `None`
   (e.g. a genuine near-zero position the provider maps to `None`) --
   while the evidence text this feeds ("Top N holdings concentration =
   X%") implies a sum over all N. Fixed to return `None` (never a
   silently-partial sum) unless every top holding has a genuine weight.
   Regression test asserts the concentration for a fully-populated set of
   top holdings against a hand-computed sum, and a separate test asserts
   `None` when any holding's weight is `None`.

**Real-data validation** (FTEC, GDX, and NVDA as an equity control, via
`SupplementalRefreshService.refresh_all`): FTEC and GDX both get `fund_
evidence` populated with full domain coverage (`1.0`) and realistic
concentration figures (~63% and ~58% of assets in their respective top 10
holdings). AI evidence coverage for both rose from what a blind equity-
style average would have produced (0.533, penalizing them for analyst
coverage they structurally can't have) to the new fund-aware 0.867. NVDA
stayed exactly byte-identical (`fund_coverage=None`, same overall coverage
as before this PR) -- confirming the `EQUITY` path is untouched. Both
ETFs' AI rating stayed `REVIEW` after the fix, confirmed to be *honest*
rather than a regression: only 2 of the 6 AI dimensions
(`valuation_context` via the new expense-ratio banding, `catalyst_
strength` via momentum/technical evidence) are genuinely assessable for a
fund under this PR's deliberately conservative dimension-mapping scope --
the other four dimensions depend on fundamental categories that are
`NOT_APPLICABLE` to a fund and have no fund-evidence substitute defined
here.

Verified live via headless browser against a locally-started dashboard:
Company Research's new Fund Evidence panel renders FTEC's expense ratio
(0.08% vs. 0.90% category average), AUM, turnover, concentration, asset
allocation, sector weightings, equity-holdings averages, and an
expandable top-10-holdings table, with zero page errors; the AI evidence
coverage caption correctly relabels "Analyst" to "Fund" for an ETF; the
Valuation Context dimension shows "Very Positive" (the expense-ratio
banding working end-to-end); and the Evidence Coverage Security Detail
tab's tracked-category count for FTEC increased by exactly one (the new
Fund Evidence row, counted as `FULL`) versus before this PR.

Full test suite (`tests/test_provider_errors.py` +2, `tests/test_
yfinance_fund_data_provider.py` new (5 tests), `tests/test_fund_evidence.py`
new (9 tests), `tests/test_supplemental_service.py` updated fake provider,
`tests/test_ai_rating.py` +8, `tests/test_coverage_summary.py` +2) and all
three established smoke tests pass.

## 28. Research Stance (PR #31)

Every prior PR added a new evidence *domain* (Analyst Consensus, Technical
Summary, AI Research Rating, Analyst Revisions, Fund Evidence, ...); none
of them synthesized across domains. The only cross-domain view that
existed was Company Research's `_render_research_summary`, an ad hoc
five-row table with no conflict detection, no Macro/External
Calibration/Revisions, and no traceability beyond a rating label. This PR
formalizes that into `alpha_lab.research_stance`, per the roadmap's PR
#31 spec: an inspectable, traceable synthesis of already-computed
evidence, never a new score.

**`alpha_lab.research_stance.stance`** (new top-level package, pure,
zero database/provider access): deliberately outside `alpha_lab.research`
-- like `alpha_lab.evidence_coverage`/`alpha_lab.alignment`, it reads
Macro Regime/External Calibration (via `alpha_lab.alignment.
AlignmentAssessment`) and (indirectly, through the caller) the News
Engine, both of which `tests/test_macro_regression.py`/`test_news_
regression.py` forbid any scoring module from importing.

`build_research_stance(research, *, news_articles=None, alignment=None)`
builds nine `StanceLine`s, mirroring `build_security_coverage_summary`'s
`news_articles`/`macro_assessment` optional-input pattern exactly:

* **Seven directional domains** -- fundamentals, analysts, revisions,
  technical, ai_research, macro, external_calibration -- each `StanceLine.
  label` is that domain's *own already-existing* categorical value,
  verbatim: `StockResearch.score_interpretation` ("Strong"/"Neutral"/...,
  already exactly the roadmap's own worked example), `AnalystConsensus.
  rating`, the nearest fiscal period's `RevisionDirection` (`revision_
  trend[0]` -- the list is sorted ascending by fiscal_period, so index 0
  is the soonest, most decision-relevant estimate; documented in `_build_
  revisions_line`'s docstring, since summarizing every period into one
  direction was deliberately out of scope), `TechnicalSummary.overall_
  rating`, `AIResearchAssessment.rating`, and `AlignmentAssessment.
  market_regime`/`donatien_lean`. This module invents no new user-facing
  terminology anywhere -- every label is a read, never a computation. A
  private `StanceLean` (POSITIVE/NEGATIVE/NEUTRAL/INSUFFICIENT_DATA)
  buckets each domain's own vocabulary for internal conflict detection
  only; it is never displayed.
* **Two non-directional domains** -- news, coverage_confidence -- have
  `StanceLine.lean = None` (not `INSUFFICIENT_DATA`; a distinct
  NOT_APPLICABLE-shaped state, mirroring `alpha_lab.research.security_
  type`'s NOT_APPLICABLE-vs-UNAVAILABLE distinction) and never
  participate in the outcome/conflict logic. News is non-directional
  because, per its own module docstring, it has "no sentiment, no
  relevance, no derived judgment of any kind" -- inventing a sentiment
  label here to fill the roadmap's illustrative "News: Neutral" example
  would be exactly the kind of fabricated conclusion this project
  refuses to produce, so News surfaces only as an evidence-count fact
  (`"N article(s)"`), same presence-only treatment `alpha_lab.evidence_
  coverage` already gives it. Coverage/confidence is a meta-statement
  about how much evidence exists across every *other* domain, not itself
  an opinion about the security -- folding it into the tally would
  double-count what it describes.

**`ResearchStanceOutcome`** (POSITIVE/MIXED_POSITIVE/NEUTRAL/MIXED_
NEGATIVE/NEGATIVE/INSUFFICIENT_DATA) is an unweighted count of directional
domains' leans -- not a blended score, per the roadmap's explicit "Do NOT
create an arbitrary weighted average": no negative -> POSITIVE, no
positive -> NEGATIVE, positive-majority -> MIXED_POSITIVE, negative-
majority -> MIXED_NEGATIVE, an exact tie is its own explicit NEUTRAL
outcome (never silently broken one way), and no domain with a definite
lean at all -> INSUFFICIENT_DATA (never forced to NEUTRAL, which would
misrepresent "no basis to judge" as "judged and balanced"). `_detect_
conflicts` lists every minority-vs-majority disagreement among directional
domains in plain English (`DOMAIN_ORDER`-deterministic, so the same inputs
always reproduce the same `conflicts`/`primary_conflict`) -- per the
roadmap's "if evidence conflicts, expose the conflict", nothing is ever
hidden inside `outcome` alone; `conflicts` is the complete, authoritative
list and `primary_conflict` (`conflicts[0]`) is a convenience, not a
replacement.

**Wiring**: Company Research's `_render_research_summary` is replaced by
`_render_research_stance_panel`, reusing that same integration point
rather than adding a redundant parallel panel. The current-research call
site fetches `NewsService(engine).get_history(ticker, limit=20)` and
`AlignmentService(engine).get_current_assessment()` (both already-
persisted, already-PIT-safe reads -- no new provider call) and passes
them into `build_research_stance`. The historical-snapshot call site
deliberately passes neither: computing them "now" for a snapshot from an
earlier `evaluation_date` would leak current-day Macro/External
Calibration/News into a supposedly point-in-time view. `_render_stock_
research` (shared by both call sites) accepts an optional `stance`
parameter and falls back to `build_research_stance(research)` (no
news/alignment) when omitted, so a snapshot still shows a stance built
entirely from its own frozen fundamentals/analyst/revisions/technical/
AI-research fields (genuinely point-in-time) with Macro/External
Calibration/News honestly reading `NOT_COMPUTED` rather than a leaked
read -- true point-in-time-correct historical Research Stance is
explicitly PR #32's job (historical validation of the new research
layers), not this one's; combining the two phases was deliberately
avoided.

**Real-data validation** (the live 11-security universe, including 4
market-wide macro-proxy tickers and 2 ETFs): `AlignmentAssessment` read
`ALIGNED`/`RISK_ON`/`CONSTRUCTIVE` market-wide, applied identically to
every ticker. NVDA/MA: all-positive directional domains (fundamentals
`Unavailable` from a config gap -- correctly `INSUFFICIENT_DATA`, never
guessed -- analysts `BUY`, revisions `IMPROVING`, ai_research `POSITIVE`,
macro/calibration both positive) -> outcome `POSITIVE`, zero conflicts.
AAL: analyst revisions `DETERIORATING` and technical `STRONG_SELL` both
against a positive macro/calibration backdrop -> outcome `NEUTRAL` (an
exact 2-vs-2 tie) with both conflicts named explicitly, e.g. "Analyst
Revisions (DETERIORATING) disagrees with positive evidence from Macro
Regime/External Calibration". FTEC/GDX (ETFs): fundamentals/analysts/
revisions/ai_research all correctly `INSUFFICIENT_DATA` (`REVIEW`/`NOT_
COMPUTED`, matching PR #29/#30's NOT_APPLICABLE-aware categories feeding
`score_interpretation`/`AnalystConsensus.rating`), technical `BUY` ->
outcome `POSITIVE`, zero conflicts. The 4 macro-proxy tickers (no
`StockResearch` fundamentals/analyst/technical/AI domains at all)
correctly show every ticker-specific line as `INSUFFICIENT_DATA`/`NOT_
COMPUTED` while still reading the shared market-wide macro/calibration
lines. Verified live via headless browser on Company Research (AAL):
zero page errors; the panel renders "Research stance: Neutral / evenly
mixed" with both conflict sentences displayed verbatim, exactly matching
the offline validation run.

**Bug caught in a final high-effort self-review pass, before opening the
PR:** `_build_analysts_line` did an unguarded dict lookup on `analyst_
consensus.rating`, which the `AnalystConsensus` model declares `AnalystRating
| None` (defaulting to `None`) even though the one production builder,
`build_analyst_consensus`, never actually produces a bare `None` (`REVIEW`
covers "couldn't be rated") -- a present-but-ratingless `AnalystConsensus`
(constructible directly, or round-tripped from a legacy/malformed stored
payload) raised an uncaught `KeyError: None`, crashing the whole Company
Research page render. The analogous Optional sub-fields on `_build_macro_
line`/`_build_external_calibration_line` were already guarded correctly;
analysts was the one line missing the same guard. Fixed by degrading to
`NOT_COMPUTED`/`INSUFFICIENT_DATA`, same as an absent `AnalystConsensus`
entirely. Regression test: `test_analysts_line_handles_a_present_
consensus_with_no_rating`.

Full test suite (`tests/test_research_stance.py` new, 24 tests covering
every per-domain lean mapping, the nearest-vs-furthest revision period
choice, non-directional News/Coverage-confidence never participating in
conflicts, all six outcome branches including the exact-tie case, the
ratingless-AnalystConsensus guard above, and that `overall_score`/
`categories` are never read back into or mutated) and all three
established smoke tests pass; `tests/test_macro_regression.py`/`test_news_
regression.py`/`test_alignment_regression.py`/`test_calibration_
regression.py` still pass unmodified, confirming the new package's
reverse-import direction (reading `alpha_lab.research` types) never
crosses back into a scoring module.

## 29. Automatic Stale-Data Refresh + Main-Page Full Refresh

Not a roadmap PR -- an operational feature requested directly: launching
the dashboard should auto-refresh core data when it's stale (never
silently, and never mid-render), and the main page should carry an
explicit manual "Full Refresh" button for the same operation on demand.

**`alpha_lab.refresh`** (new top-level module, mirrors `alpha_lab.data_
quality`'s flat placement for a similarly cross-cutting operational
concern): the ONE canonical "core refresh" code path, reused by both
callers below rather than duplicated.

* `is_universe_price_stale(engine, stale_after_days)` -- a pure database
  read (`SELECT ticker, MAX(date) ... GROUP BY ticker`, never a full
  price-history table scan, since this runs unconditionally on every
  dashboard render) reusing the *existing* `settings.data_quality
  ["stale_price_days"]` observation limit and `alpha_lab.data_quality.
  assess_freshness`, both already in production use for the Data Quality
  section's per-ticker display. An empty universe (nothing tracked yet)
  is never "stale" -- that is a separate bootstrapping concern.
* `run_core_refresh(engine, settings)` -- ingests price/fundamental data
  for exactly the already-tracked universe (`configured_universe_tickers`,
  the same `Security` rows `MarketScreenerService.build_live_records`
  itself reads, never a rediscovered/expanded universe) via `alpha_lab.
  ingestion.IngestionService`, then calls `MarketScreenerService.
  rebuild_current_research()`. Deliberately narrow scope, per explicit
  instruction: never the supplemental research domains (Analyst
  Consensus/Technical/AI Research/News/Macro Regime/Donatien External
  Calibration) -- those remain independently refreshable through their
  own existing `scripts/refresh_*.py` mechanisms. One ticker's *provider*
  failure never aborts the rest (mirrors `scripts/load_us_data.py`'s
  `_ingest_universe` exactly -- only `ProviderError` is caught per
  ticker); the rebuild step's own failure is caught and reported on
  `CoreRefreshResult.research_error` rather than propagated, and can
  never corrupt or partially overwrite the previously persisted current
  research, since `Phase3Repository.save_current_research` already
  validates before opening a session and persists one immutable build
  atomically.
* `run_core_refresh_guarded(engine, settings, state)` -- refuses to start
  a second refresh while `state` (typically `st.session_state`) already
  records one in progress, returning `None` instead of overlapping. This
  guard is explicitly scoped to one session/process: it does not (and by
  design does not attempt to) prevent two different browser sessions from
  each starting their own core refresh concurrently -- true cross-session
  locking was explicitly ruled out as unnecessary complexity for this
  change (partially-refreshed state, concurrent provider calls, and
  ambiguous UI consistency are exactly what staying synchronous/atomic
  and narrowly scoped avoids; a backgrounded "Refresh Supplemental
  Research" workflow with its own progress/status handling is left for a
  future, separate phase).

**`scripts/launch.py`** (new): checks `is_universe_price_stale` once
before the Streamlit server ever starts, runs `run_core_refresh` only if
stale, then execs `streamlit run app/dashboard/main.py` regardless of
whether that refresh fully succeeded -- a failed or partial refresh never
blocks the dashboard from launching with whatever valid data is already
persisted. Deliberately outside Streamlit's own process: `main.py` is
rerun by Streamlit on every widget interaction, and this check/refresh
must run exactly once, before the server starts, never on every rerun.

**`app/dashboard/main.py`**: a "🔄 Full Refresh" button calls `run_core_
refresh_guarded(engine, settings, st.session_state)` directly inside its
`if st.button(...):` block -- the only place in this file that ever
invokes a provider; every other render remains a pure read, exactly like
the rest of this codebase's explicit-refresh architecture. A stale-data
warning banner (reusing `is_universe_price_stale`) sits above it for
context. After the button's result is shown, `build_screener.clear()`
invalidates the cached screener DataFrame so the *rest of this same
script run* (the `build_screener()` call further down the page) picks up
fresh data -- deliberately no `st.rerun()` afterward: Streamlit already
runs the script top-to-bottom on the click that triggered this, and an
explicit rerun would restart the script before the user could read the
success/error message (on the fresh run `st.button(...)` returns `False`,
so the message block never re-executes and Streamlit silently drops the
prior run's element).

**Bugs caught in a high-effort self-review pass, before validation:**

1. The Full Refresh handler originally called `st.rerun()` right after
   displaying `st.success()`/`st.error()`, immediately restarting the
   script and dropping the just-shown message before the user could read
   it -- contradicting this codebase's own established pattern elsewhere
   (Company Research's/Macro Regime's refresh buttons deliberately omit
   `st.rerun()` for exactly this reason). Fixed by removing it; clearing
   the cache alone is sufficient since `build_screener()` is called later
   in the same run.
2. `_latest_price_by_ticker` pulled every stored `Price` row into Python
   just to find each ticker's latest date, and `is_universe_price_stale`
   runs unconditionally on every dashboard render -- a full price-history
   table scan on every page load/rerun. Fixed to use `SELECT ticker,
   MAX(date) ... GROUP BY ticker`, returning one row per ticker.
3. The module docstring and `run_core_refresh_guarded`'s docstring
   originally implied "no concurrent provider calls" as an unconditional
   guarantee; the guard is actually scoped to one `state` mapping (one
   Streamlit session), not cross-session/cross-process. Corrected both
   docstrings to state the guarantee's actual scope rather than overclaim
   it, instead of expanding the implementation to match the overclaim --
   true cross-session locking was explicitly out of scope for this change.

**Validation**: full test suite (`tests/test_refresh.py` new, 12 tests
covering fresh-data-skips-refresh, stale-data-triggers-exactly-one-
ingestion-call-per-ticker, the configured-universe/rebuild-read ticker
set matching exactly, a failed rebuild never touching previously
persisted current research, a per-ticker provider failure never aborting
the rest or the rebuild, a normal module import/rerun of `main.py` never
calling `run_core_refresh` even when patched to raise on any call, the
Full Refresh path delegating to the same `run_core_refresh` provider
calls, a refresh already in progress never duplicated, and the
in-progress flag always clearing even when the refresh raises) and all
three established smoke tests pass. `git diff --check`: clean. Real-data
validation against the live database: `is_universe_price_stale` correctly
reads `True` (the tracked universe's price data is in fact older than the
configured 5-day limit), matching the pre-existing "stale price" Data
Quality flag already shown elsewhere for the same tickers. Verified live
via headless browser on the main dashboard: zero page errors; the stale-
data warning banner and Full Refresh button both render correctly,
matching the offline validation exactly. A real network-backed refresh
was deliberately not triggered during validation (to avoid unnecessary
yfinance rate-limit usage) -- the underlying ingestion/rebuild path is
exhaustively covered by the fake-provider unit tests above and is the
exact same `IngestionService`/`MarketScreenerService.rebuild_current_
research` path every other real-data-validated PR in this document
already exercises.

**Bug caught in external review of this PR, before merge:** `run_core_
refresh` deliberately only isolates two failure modes -- a per-ticker
`ProviderError` (mirroring `scripts/load_us_data.py`'s established
`_ingest_universe` pattern) and a `rebuild_current_research` failure --
both land on the returned `CoreRefreshResult` rather than raising. An
infrastructure-level failure outside those two paths (e.g. the database
itself becoming unreachable mid-loop) was deliberately left to propagate,
matching that same precedent. But `scripts/launch.py`'s own docstring
promised something broader -- "launches Streamlit regardless of whether
that refresh fully succeeded" -- and nothing enforced that promise for
this specific case: an unexpected exception from `run_core_refresh`
propagated straight out of `main()`, so `os.execvp(...)` was never
reached and the dashboard never launched at all, the opposite of the
documented contract. Fixed by wrapping the `run_core_refresh` call itself
in `scripts/launch.py` with its own broad exception handler -- the one
place that broader promise is actually kept, without changing `run_core_
refresh`'s precedent-matching, precisely-scoped exception handling.
The Full Refresh button had the same gap (an unexpected exception would
have hit Streamlit's default traceback UI instead of the same graceful
`st.error(...)` every other refresh failure gets); fixed the same way,
at the button's own call site. Regression tests:
`tests/test_launch.py` (new, 4 tests, including `test_launch_still_
execs_streamlit_when_core_refresh_raises_unexpectedly`, which reproduces
the original bug -- `os.execvp` was never called -- and confirms the fix).

## 30. Historical Validation of New Research Layers (roadmap PR #32)

Not a new evidence domain -- the roadmap's next phase after Research
Stance, explicitly a validation pass: "Could the research state on date X
have accessed information that was only published after date X? If yes,
fix the provenance boundary." Every research layer added since PR #26
(Analyst Consensus, Analyst Rating Changes/Revision Trend, Technical
Summary, AI Research Rating, News, Macro Regime/External Calibration,
Fund Evidence, Research Stance) and the research-snapshot mechanism
itself were audited against that question.

**Snapshot mechanism (clean).** `alpha_lab.research.snapshots.
ResearchSnapshotRepository` already persists one immutable, fully-
materialized `StockResearch` payload per snapshot (`model_dump(mode=
"json")` of everything -- categories, `analyst_consensus`, `technical_
summary`, `ai_research_assessment`, `analyst_research`, `fund_evidence`).
`get`/`get_latest`/`list_for_ticker` deserialize that frozen payload
directly and never re-join against any `Current*` table, so reading an
old snapshot can never pick up today's data -- current-vs-historical
separation holds structurally, not by convention. Idempotent on content
(`payload_hash`-derived `snapshot_id`), confirming snapshot reproducibility.
PR #31's own historical-snapshot rendering path (Company Research's
Research Stance panel) already omits Macro/External Calibration/News for
a snapshot view for the same reason, rather than re-fetching them "now" --
verified consistent with this mechanism's own guarantee.

**News / Macro Regime / External Calibration (already correct, confirmed
by re-reading, no change).** `NewsService.get_history(as_of=...)` filters
`retrieved_at <= end_of(as_of)`; `MacroRegimeService.get_assessment_as_of`/
`ExternalCalibrationService.get_calibration_as_of` follow the identical
`retrieved_at`-based pattern. All three already correctly treat the
source's own claimed date (`published_at`, a market date) as evidence to
display, never as the eligibility test for what a historical `as_of` read
may see.

**Technical Summary / Analyst Consensus / AI Research Rating / Fund
Evidence (no leak, but no historical replay capability either).** Each is
a single upserted `Current*` row per ticker with no `as_of` parameter
anywhere in `SupplementalResearchService`'s read methods -- there is
nothing to leak because there is no historical query surface at all yet.
This is a real capability gap, not a bug: reconstructing what any of
these looked like on a past date is exactly the *next* roadmap phase's
job ("historical research reconstruction... where source coverage
permits"), not this one's.

**Bug found and fixed: `AnalystEventsService`'s `as_of` parameter was
not point-in-time-safe.** `get_research_summary(ticker, as_of=...)`
accepted `as_of` and stamped it onto the output and the 90-day rating-
change tally's window start -- but the two underlying reads it actually
calls, `get_rating_changes`/`get_latest_revision_trend`, took no `as_of`
at all and always returned every stored row regardless. yfinance's
`upgradeDowngradeHistory`/`earningsTrend` each backfill a ticker's entire
history in one refresh call (see `AnalystRatingChange`/`EstimateRevisionTrend`'s
own docstrings), so a row whose claimed `grade_date`/`observation_date` is
years in the past can still have been inserted into these tables only
today. A caller requesting `get_research_summary(ticker, as_of=<a past
date>)` would have silently received rating changes and revision-trend
observations AlphaLab had not actually ingested until long after that
date -- exactly the future-information leak this phase exists to catch,
and precisely the trap the next roadmap phase (historical research
reconstruction) would have inherited had it reached for this parameter
trusting it was already safe.

Fixed by filtering both reads on the field that actually records when
AlphaLab itself stored the row -- `AnalystRatingChange.retrieved_at` and
`EstimateRevisionTrend.ingested_at` (a genuine ingestion timestamp,
distinct from `observation_date`, which is merely the refresh call's own
`as_of` argument and so cannot be trusted for a read-side PIT filter) --
mirroring `NewsService.get_history`'s established `retrieved_at`-based
pattern exactly, rather than filtering on the source's claimed
`grade_date`/`observation_date`. `get_research_summary` now passes `as_of`
through to both reads unconditionally, so `build_analyst_research_summary`
never sees a row AlphaLab hadn't yet ingested. Omitting `as_of` (the
current-research path every existing caller uses) is completely
unaffected -- verified byte-identical against the live database.

**Two further self-review findings on that same fix, both resolved
before opening this PR:**

1. *Per-period "latest" selection was still ordered by the untrusted
   field.* The new `ingested_at`-based `WHERE` clause correctly excluded
   not-yet-ingested rows, but `get_latest_revision_trend`'s subsequent
   "pick the newest row per `fiscal_period`" step still ordered by
   `observation_date` -- the same caller-controlled field the fix's own
   rationale says cannot be trusted. Two rows for the same period can
   have an `observation_date` order that disagrees with the order
   AlphaLab actually ingested them in, silently picking the wrong "latest"
   row. Fixed by ordering that selection by `ingested_at` too, consistent
   with the rest of the fix. Regression test added: two observations for
   one `fiscal_period` with `observation_date` and `ingested_at` order
   deliberately reversed, confirming the `ingested_at`-latest row wins.
2. *Tz-aware vs. naive datetime comparison.* `AnalystRatingChange.
   retrieved_at`/`EstimateRevisionTrend.ingested_at` default to a
   tz-aware `datetime.now(UTC)`, while the new `as_of` upper bound is a
   naive `datetime.combine(as_of, time.max)` -- unlike `NewsArticleRecord.
   retrieved_at`, which is explicitly stripped of tzinfo before storage
   (`alpha_lab/news/service.py`). Verified empirically (a throwaway
   in-memory-SQLite script, not just read) that this is not a bug:
   SQLAlchemy's plain `DateTime` column on SQLite discards tzinfo on
   write regardless, so both sides always compare as naive UTC wall-clock
   values. No behavior change; docstrings note this explicitly so a
   future reader doesn't have to re-derive it.

**Deliberately not done:** no change to the backtest/trading engine, and
the new research layers are not injected into `HistoricalScoringService`
or any strategy path -- per the roadmap, that decision is explicitly
deferred to a future phase, decided from what this validation actually
found, not assumed.

**Real-data validation** (live database): `get_research_summary("NVDA",
as_of=date(2020, 1, 1))` -- a date before this environment's data was
ever ingested -- correctly returns `None` (nothing had genuinely been
ingested by then), while `get_research_summary("NVDA")` (current) and
`get_research_summary("NVDA", as_of=date.today())` both correctly return
the full current view (20 rating changes, 2 revision-trend periods),
identical to before this fix -- confirming the filter excludes exactly
what it should and nothing more.

Full test suite (`tests/test_analyst_events_service.py` +4: two direct
tests proving `get_rating_changes`/`get_latest_revision_trend` exclude a
row whose `retrieved_at`/`ingested_at` postdates the requested `as_of`
while including it once `as_of` catches up, one end-to-end
`get_research_summary` test, plus one regression test for the
per-period-selection finding above; one pre-existing test's incidental
`as_of` argument corrected since it was unrelated to what that test
actually verifies) and all three established smoke tests pass.
`git diff --check`: clean.

## 31. Automatic On-Session-Start Refresh of Stale-Only Data

§29's Full Refresh button required a manual click even when data was
already known to be stale (the page already computes and displays that
fact on every render). This closes that gap: on the first render of a
browser session, if any already-tracked ticker's price data is stale, the
dashboard now automatically refreshes exactly those stale tickers -- no
click required -- before the page finishes that same render. The Full
Refresh button is unchanged and still available for an on-demand refresh
of the entire universe.

**Deliberately scoped to the stale subset, not the whole universe.**
Unlike the launch-time check (`scripts/launch.py`) and the Full Refresh
button, both of which re-ingest the full configured universe, this
automatic trigger only re-ingests tickers `stale_universe_tickers` (new;
`is_universe_price_stale`'s boolean refactored to use it) actually
reports as stale -- otherwise every single browser session's first page
load would pay for a full-universe refresh merely because *one* ticker
happened to be stale, an unbounded and unnecessary cost the user
explicitly flagged when scoping this feature. `run_core_refresh`/
`run_core_refresh_guarded` both gained an optional `tickers: list[str] |
None` parameter for this; omitting it (every existing caller) is
byte-for-byte the prior full-universe behavior -- purely additive, no
existing test needed to change.

**Guarded to fire at most once per browser session,** via a new
`st.session_state["auto_stale_refresh_attempted"]` flag set unconditionally
the first time this code runs (whether or not anything was actually
stale, and whether or not the attempt succeeded) -- a later rerun
triggered by any widget interaction must never re-attempt it, and a
failed automatic attempt does not retry itself; the Full Refresh button
remains available to retry manually. This is a distinct flag from
`run_core_refresh_guarded`'s own `core_refresh_in_progress` guard, which
only prevents two *overlapping* refreshes, not a second sequential one.

**Bug found and fixed while testing this feature (via `streamlit.testing.
v1.AppTest`, not just unit-testing `run_core_refresh` in isolation):** the
existing staleness banner (added in §29) was computed and rendered
*before* the Full Refresh button's own refresh logic ran later in that
same script execution. A successful button click therefore still showed
the "data is stale" warning immediately above its own "Ingested .../
research rebuilt" success message in that identical page render --
self-contradictory, even though the underlying staleness computation was
always correct (a *subsequent* rerun always showed the fixed state; the
bug was purely that the first render never got the chance to reflect a
refresh that had just happened later in it). Confirmed by actually
clicking the button via `AppTest` and inspecting the rendered elements --
this class of bug is invisible to a plain `importlib` module exec (as
`tests/test_dashboard_main_screener.py` uses), since `st.button()` always
returns `False` outside a live `ScriptRunContext`. Fixed by rendering the
banner into an `st.empty()` placeholder and re-rendering it in place after
any refresh (automatic or button-triggered) completes within the same
run, rather than only ever rendering it once at the top of the script.

**Incidental fix, found via the same `AppTest` exploration, unrelated to
either refresh path:** `screen["Overall Rating"].fillna(-1) >= minimum`
(the Stock Screener's rating-threshold filter, pre-existing) raised a
pandas `FutureWarning` whenever every row's `Overall Rating` was `None`
(the column's dtype becomes `object` with nothing to promote it to
`float64`, and `.fillna()` silently downcasting an object-dtype array is
deprecated). The comparison's *result* was unaffected either way (`-1 >=
minimum` is always `False` for any non-negative slider value, verified
against pandas' `future.no_silent_downcasting` opt-in), so this was a
forward-compatibility warning rather than a live correctness bug, but
still real and reproducible. Fixed by coercing with `pd.to_numeric(...,
errors="coerce")` before `.fillna(-1)`, so the column is already
`float64` by the time `fillna` runs and never needs to downcast.

**Real-data validation** (live database, read-only -- no network call
made): `stale_universe_tickers`/`is_universe_price_stale` agree exactly
(11/11 tracked tickers currently stale in this environment), and the
stale set is confirmed a subset of the full configured universe.

New test file `tests/test_dashboard_full_refresh_banner.py` (4 tests,
using `AppTest` to actually run the page and click the button, not a
plain module exec): the banner-ordering fix in isolation from the new
automatic trigger; the automatic trigger fixing a stale ticker within its
own first run and never firing twice; the automatic trigger touching only
the stale subset when a fresh ticker sits alongside it in the universe;
and a fully-fresh universe never triggering it at all. Full test suite
and all three established smoke tests pass. `git diff --check`: clean.

## 32. Historical Research Reconstruction (roadmap)

Roadmap's next phase after Historical Validation (§30): where §30 audited
that no new research layer leaks future information into a historical
`as_of` read, this phase adds the actual reconstruction capability for the
domains §30 identified as having "no leak, but no historical replay
capability either" -- Technical Summary, Analyst Consensus, AI Research
Rating, and Fund Evidence. Scoped explicitly (per direction): cover all
four domains now; for the three with no history table of their own,
"reconstruction" means starting genuine forward-only snapshotting rather
than fabricating anything retroactively, keeping the automatic trigger's
cost proportional to what a user actually does (not a blanket background
job) -- and, distinctly, Technical Summary needs no snapshot at all.

**Technical Summary: reconstructed on demand, not snapshotted.** Unlike
the other three, Technical Summary is a pure function of `Price` history
(`build_technical_summary`, unchanged), and Price history genuinely
accumulates day by day regardless of whether anyone ever refreshes
anything -- so any past date within that history is reconstructible
directly. New `SupplementalResearchService.get_technical_summary_as_of
(ticker, as_of)` filters on `Price.ingested_at <= end_of(as_of)`, never on
`Price.date` alone -- `run_core_refresh` can backfill up to two years of
history in a single call, so a bar dated years in the past can still have
been inserted only today; filtering on `date` alone would leak that
not-yet-ingested history into a historical read, exactly the trap §30's
own `AnalystEventsService` fix exists to warn against. Always returns a
`TechnicalSummary` (never `None`): a short/excluded price window yields an
honest REVIEW rating with zero coverage, matching `refresh_technical_
summary`'s own "always succeeds" guarantee. `refresh_technical_summary`
and the new method now share one `_price_history_frame` helper so the two
computations can never silently drift apart on what counts as a usable
price bar.

**Analyst Consensus / AI Research Rating / Fund Evidence: no new tables --
the existing `StockResearch` snapshot mechanism already carried them.**
Auditing `ResearchService.build_research_for_record` found it already
enriches `StockResearch` with the *current* value of all three (plus
Technical Summary and Analyst Research) on every call, and `persist_
snapshot` already freezes whatever `StockResearch` it is given -- so a
snapshot saved at any moment already captures all three domains' state at
that moment. The only actual gap was that persisting a snapshot required
a separate, easy-to-forget manual "Save research snapshot" click; nothing
made it happen automatically alongside an actual refresh. Building three
new dedicated snapshot tables (the original plan) would have duplicated a
mechanism that already existed and already round-trips these fields
(`tests/test_research_snapshots.py`'s existing supplemental-domain
coverage).

Fixed with one new method, `ResearchService.snapshot_current_research
(ticker)`: re-reads current research and calls `persist_snapshot` on it,
or does nothing if there is none. Idempotent (`persist_snapshot` dedupes
identical content), so a refresh that changed nothing never creates a
duplicate. Both of this codebase's supplemental-refresh entry points call
it right after their own refresh completes: Company Research's "Refresh
for this ticker" button, and `scripts/refresh_supplemental_research.py`'s
batch run (every branch of it -- normal, `--skip-analyst`, and "no
fundamental research yet", not only the common case). The pre-existing
manual "Save research snapshot" button is unchanged and still useful on
its own (e.g. to mark a fundamental-score-only change with no
supplemental refresh).

**Self-review finding, fixed before this PR:** the first version of this
wired the automatic snapshot directly into the Company Research page's
button handler only (re-reading `research` and calling `persist_snapshot`
inline there), never into the batch script -- which is plausibly the
*primary* way supplemental research actually gets refreshed for a whole
universe in practice (its own docstring: refresh all three domains for US
securities, one call). Left as-is, the script's routine/nightly use would
have kept building zero history, silently defeating "history accumulates
from ordinary use" for its own main usage path. Fixed by extracting the
shared `snapshot_current_research` method above and calling it from both
places instead of only the UI. A second, smaller finding from the same
review: `get_technical_summary_as_of`'s `Price` query filtered rows dated
after `as_of` in Python after fetching them, rather than in the same SQL
`WHERE` clause -- harmless today (backfills are capped at two years) but
needlessly pulling rows across the DB connection just to discard them;
fixed by adding `Price.date <= as_of` to the query itself.

**New point-in-time snapshot lookup.** `ResearchSnapshotRepository.
get_latest_as_of(ticker, as_of)` / `ResearchService.get_latest_snapshot_
as_of` return the most recently *persisted* snapshot that genuinely
existed by `as_of` -- filtered on `ResearchSnapshot.created_at`, the row's
own real persistence timestamp, never on `evaluation_date` (the research's
own claimed date), mirroring every other PIT read in this codebase.
Returns `None` when nothing had been saved for that ticker by that date --
an honest capability gap for any date before automatic/manual
snapshotting started, never fabricated. The existing per-snapshot browsing
UI (pick a saved snapshot by its own label) was already sufficient to
*view* a specific known snapshot; this adds the "what was the state as of
this date" query the roadmap phase is actually about.

**UI**: Company Research gained a "Reconstruct research as of a past
date" section -- a date picker + button that calls `get_latest_snapshot_
as_of` for Analyst Consensus/AI Research Rating/Fund Evidence and
`get_technical_summary_as_of` for Technical Summary (recomputed exactly
as of the chosen date, not just as of the nearest snapshot's own date,
since it can be), rendering both through the existing shared
`_render_stock_research`/`_render_technical_summary_panel` functions
already used for the current-research and per-snapshot views -- no new
rendering logic, just a new way to reach it. When no snapshot exists yet
for the requested date, it honestly says so and falls back to showing
Technical Summary alone (which never depends on a snapshot).

**Deliberately not done:** no change to `HistoricalScoringService` or any
backtest/strategy path -- these four domains remain supplemental research
evidence only, exactly as established in every prior phase that touched
them.

**Validation:** new tests in `tests/test_research_snapshots.py` (`get_
latest_as_of`: none-yet, most-recent-not-after, and a `created_at`-vs-
`evaluation_date` PIT regression), `tests/test_supplemental_service.py`
(`get_technical_summary_as_of`: matches the live computation when nothing
is excluded, excludes not-yet-ingested price rows, excludes bars dated
after `as_of` even when already ingested, and never returns `None`),
`tests/test_research_service.py` (thin-wrapper passthrough), and new
`tests/test_refresh_supplemental_research.py` (the self-review fix above:
`scripts/refresh_supplemental_research.py`'s `main()` persists an
automatic snapshot for a refreshed ticker, both in its normal path and
its `--skip-analyst` path). Full test suite and all three established
smoke tests pass. `git diff --check`:
clean. Explicitly scoped out of the permanent test suite: full `AppTest`
coverage of the Company Research page's new UI section -- that page has
far more moving parts than the main dashboard, and the new logic there is
a few lines of glue over already-tested methods; instead it was validated
exploratorily via `AppTest` (seeded a ticker, clicked "Refresh for this
ticker", confirmed the automatic snapshot caption and no exception,
exercised "Reconstruct" both for a date with no snapshot yet, correctly
falling back to Technical-Summary-only, and for today, correctly finding
the just-created snapshot) rather than checked in as a maintained test.
Real-data validation (live database, read-only, no network call):
`get_latest_snapshot_as_of("NVDA"/"MA"/"AAL", as_of=2020-01-01)` all
correctly return `None` (nothing had been ingested that far back in this
environment), while `get_technical_summary_as_of(ticker, date.today())`
matches the live `get_technical_summary` computation exactly (same
coverage, same rating) for all three.

## 33. Signal Predictive-Value Study (roadmap)

Roadmap's next phase after Historical Research Reconstruction (§32): per
the roadmap's own explicit framing at Historical Validation (§30) --
"first establish historical correctness [then] a future separate phase
can investigate whether research signals have predictive/decision value"
-- this measures, read-only, whether any supplemental signal correlates
with a ticker's own subsequent price return. **Never wired into
`HistoricalScoringService` or any scoring/backtest path** -- this only
measures whether a signal *would have* said anything, it does not make
that happen; that remains a separate future decision either way.

**Scope, decided explicitly before writing any code:**

- **Technical Summary** is the only domain analyzed by default -- the
  only one *capable* of testable historical depth, since it is a pure
  function of `Price` history (`get_technical_summary_as_of`), which
  accumulates day by day regardless of whether anyone ever refreshes
  anything.
- **Analyst Consensus / AI Research Rating** have no historical depth of
  their own yet -- their only history is whatever `ResearchSnapshot` rows
  §32's `snapshot_current_research` has recorded, which is close to zero
  per ticker the moment that phase shipped. The methodology for
  correlating them (`collect_analyst_consensus_observations`/
  `collect_ai_research_assessment_observations`, using `AnalystConsensus.
  rating_score`/`AIResearchAssessment.score` -- both already numeric, no
  ordinal re-encoding needed) is fully designed and implemented, but each
  deliberately raises `InsufficientSnapshotHistory` below
  `MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE` (30) real `(ticker, snapshot)`
  pairs, rather than silently computing a correlation from a handful of
  near-simultaneous snapshots and reporting it as if it meant something.
  No code change is needed to "enable" them later -- they start working
  correctly the moment enough real history has accumulated.
- **Fund Evidence is excluded by design, not data depth.** Unlike the
  other three, it has no ordinal rating/score field at all (see
  `FundEvidence`) -- purely descriptive (asset allocation, sector
  weightings, operations, holdings), with no single bullish/bearish
  reading to correlate against a forward return. A derived signal (e.g.
  concentration or expense-ratio percentile) is a genuinely new research
  question for a future phase, not this one's "does the existing signal
  predict anything" -- so it is left out entirely rather than forcing an
  artificial score onto it.

**Methodology:** Pearson and Spearman correlation between a signal's
value at each sampled historical date and the ticker's own actual forward
return from that date, using AlphaLab's own stored `Price` history for
the forward return -- no PIT filtering needed on that side, since it asks
"what actually happened next in reality" (already-established fact),
never "what did AlphaLab know" (which is what the *signal* side must
still get right, via the same PIT-safe `as_of` reads §30-32 established).
Reported descriptively only (`pearson`/`spearman`/`sample_size`), never
with a computed "significant" verdict -- see the external review finding
below for why. No `scipy` dependency (not otherwise used in this
codebase): Spearman is computed as Pearson correlation of the
rank-transformed values (`Series.rank()`, pure pandas/numpy), which is
mathematically identical to pandas' own `method="spearman"` but avoids
the transitive scipy dependency that path silently pulls in.

**Self-review findings, fixed before this PR:**

1. The Technical Summary path had no minimum-observation gate at all,
   unlike the two snapshot-based domains -- in exactly the sparse-history
   scenario this phase's own real-data run below hit, a handful of
   surviving (non-REVIEW) samples could produce a technically-large `|r|`
   flagged `approx_significant=True` purely by chance, the identical
   spurious-significance trap `MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE` was
   introduced to prevent for the other two domains. Fixed by moving the
   sample-size gate into `_correlate` itself, applied uniformly to every
   domain -- Technical Summary still always reports whatever it computed
   (it never raises `InsufficientSnapshotHistory`), it just never flags a
   tiny sample as significant.
2. The module's own docstring claimed "no scipy dependency", but
   `pandas.Series.corr(method="spearman")` transitively requires scipy at
   runtime -- verified by actually blocking the import and reproducing
   the crash. scipy happens to be installed in this environment but is
   not a declared project dependency and is used nowhere else in
   `alpha_lab`; a future contributor auditing "unused" pinned packages
   could reasonably drop it, silently breaking this module. Fixed via the
   rank-transform approach above (verified correct against pandas' own
   Spearman on a hand-checked example, and verified to still work with
   scipy's import actively blocked).

**External review findings, fixed after opening this PR:**

3. **The significance flag treated repeated snapshots as independent
   observations.** `|r| > 2/sqrt(n)` assumes `n` independent samples, but
   `_collect_snapshot_domain_observations` counts every persisted
   `ResearchSnapshot` for a ticker as one observation regardless of how
   close together in time they were recorded -- two snapshots a day apart
   have forward-return windows overlapping in all but one day, which is
   the classic clustered/repeated-measures problem (see e.g.
   `statsmodels`' GEE estimator, built for exactly this). Treating that
   raw count as `n` in the threshold overstates confidence. Rather than
   build the clustering-aware correction this would need (minimum
   snapshot spacing, or a cluster-robust estimator) for what is still a
   near-zero-history, exploratory phase, `approx_significant` was removed
   entirely -- `CorrelationResult` now reports `pearson`/`spearman`/
   `sample_size` descriptively and leaves the judgment of whether that is
   compelling to whoever reads the result, for every domain including
   Technical Summary (which has no clustering concern of its own, since
   `sample_interval_days` already keeps its own samples non-overlapping,
   but shares the same reporting shape for consistency).
4. **The snapshot-domain forward-return anchor could use a price not yet
   known at snapshot time.** `_collect_snapshot_domain_observations` used
   `entry.created_at.date()`'s own closing price as the entry price for
   the forward return -- but unlike Technical Summary's `as_of` (a
   deliberate end-of-day reconstruction boundary), `entry.created_at` is a
   real, uncontrolled intraday timestamp: a snapshot recorded mid-session
   (e.g. 11:00) could be paired with that same day's own close, which
   plainly was not yet known at that moment. Fixed by anchoring on the
   first trading day's close strictly after the snapshot date instead
   (`prices.index.searchsorted(..., side="right")`) -- Technical Summary's
   own sampling is untouched, since its `as_of` already represents "known
   by end of this day" by construction, not a raw event timestamp.

**Real-data run** (live database, read-only, no network call) -- an
important, genuine finding discovered by actually running the study, not
a bug to fix: every domain reports **zero testable observations today**,
for two related-but-distinct reasons. Technical Summary: this
environment's entire multi-year `Price` history was bulk-backfilled in
one real moment (`run_core_refresh` ingests up to two years per call), so
every row shares roughly one `Price.ingested_at` regardless of how far
back its `date` is -- every sampled historical date still predates that
real ingestion moment, so `get_technical_summary_as_of` correctly (not a
bug) returns REVIEW for all of them, exactly as PIT-safety demands.
Analyst Consensus / AI Research Rating: `InsufficientSnapshotHistory`, as
designed (§32's automatic snapshotting only just started). Both amount to
the same underlying constraint -- **this phase needs real elapsed
wall-clock time with live, incremental usage, not more code** -- and the
correlation study working correctly is exactly what surfaces that
honestly instead of fabricating a number. Per the agreed methodology
("start with correlation; only build the backtest-overlay comparison for
a signal that passes that first filter"), since nothing passed the filter
today, **no backtest-overlay comparison is built in this phase** -- there
is nothing valid to run it against yet. Re-running `scripts/analyze_
signal_predictive_value.py` after enough real time has passed (this
environment's own next auto-refresh cycles onward) is expected to start
producing real Technical Summary results; Analyst Consensus/AI Research
Rating need enough "Refresh for this ticker"/batch-script runs to clear
30 observations across the universe.

**Deliberately not done:** no scoring/backtest integration of any kind,
regardless of what a future correlation run finds -- that remains a
separate decision. A known, accepted minor inefficiency, not fixed here:
running the Analyst Consensus and AI Research Rating studies back to back
(as the CLI script does) re-walks each ticker's `ResearchSnapshot` history
and re-deserializes the same payloads twice; not worth the added
complexity while both return zero real observations in every environment
that has run this so far.

**Validation:** `tests/test_signal_predictive_value.py` (12 tests) --
`_correlate`'s own math (perfect positive/negative correlation, a small
pearson for pure noise, too few observations returns `None` not a crash,
and confirmation `CorrelationResult` reports its true `sample_size`
without any `approx_significant` attribute even for a technically-perfect
correlation from 5 points), `collect_technical_summary_observations`'
pairing/skip logic (correct forward-return pairing via a hand-computed
example, REVIEW samples skipped, short-history and untracked tickers
produce zero observations without crashing) via a monkeypatched
`SupplementalResearchService` isolating this module's own logic from
indicator computation (already exhaustively tested in `tests/
test_technical_summary.py`), `collect_analyst_consensus_observations`
raising below the threshold / succeeding once enough real snapshots exist
(seeded directly, mirroring this session's established `_set_created_at`
PIT-testing pattern), and the external-review regression: a snapshot
recorded mid-session is paired with the next trading day's close, never
that same day's own close. Full test suite and all three established
smoke tests pass. `git diff --check`: clean.

## 34. Production-Scale Performance Diagnosis (main page + Universe Breakdown)

A real deployment reported the main dashboard stuck on "Automatically
refreshing 3931 stale ticker(s)..." for over 10 minutes, plus a separate
report that the Evidence & Coverage Dashboard's Universe Breakdown tab was
also slow. Both were diagnosed against the actual code paths (and, for
the second, an actual profiling run) rather than guessed at.

**Main page: expected, not a hang -- but a real design gap in §31's own
safety intent.** At 3931 tickers, `run_core_refresh` makes one live
provider round-trip per ticker (company info + price history +
financials -- three HTTP calls each), entirely serial, with no
backgrounding; realistically that alone is well over an hour at thousands
of tickers, before `IngestionService.ingest`'s own per-row database
upsert-check (one `SELECT` per stored price row, so up to ~500 more
queries per ticker for a 2-year window) adds further time on top. So "10+
minutes and still going" was not stuck -- it was working exactly as
built, just at a scale the automatic trigger was never actually
guarded against. §31 introduced this automatic on-session-start trigger
specifically to keep cost "proportional to what is actually stale," but
never anticipated *almost the entire universe* going stale at once (e.g.
after the app sits unused past `stale_price_days`, or a freshly-loaded
large universe) -- at that scale the trigger silently turns into a
many-hour blocking page load, the opposite of its own design goal.
Fixed with `alpha_lab.refresh.MAX_AUTO_REFRESH_TICKERS` (200): above this
many stale tickers, the automatic trigger is skipped entirely -- the
existing stale-data warning and manual Full Refresh button remain how to
catch up on demand, refreshing everything is still possible, it is just
never silently automatic at that scale. Below the cap, behavior is
unchanged from §31.

**Universe Breakdown: a real, separate N+1 query pattern -- but not the
main page's own bottleneck.** `_load_universe_coverage_rows` (`app/
dashboard/pages/8_Evidence_Coverage.py`) already correctly fetches the
universe once (`list_current_research`) and avoided the documented O(n²)
`get_stock_research`-per-ticker trap, but still called `NewsService.
get_history(ticker)` -- one full database round-trip -- inside its
per-ticker loop. Fixed with a new `NewsService.get_history_for_tickers`
(one `ticker IN (...)` query, grouped by ticker in Python, same PIT
semantics as `get_history`'s own `as_of`). A profiling run (`cProfile`
against a synthetic 2,000-ticker universe, in-memory SQLite) confirmed
this alone is not what a 10-minute complaint would be about: `build_
research_for_record`'s own per-ticker enrichment (`SupplementalResearchService`'s
four `Current*` lookups plus `AnalystEventsService`'s reads) dominates
instead, at roughly ~2ms/ticker -- each read opens its own `Session`, so
the fixed per-call overhead of session/connection setup, not the actual
SQL, is what adds up. At ~4,000 tickers that is on the order of seconds,
not minutes -- real and worth knowing, but not remotely the same order of
magnitude as the main page's issue above. Deliberately not fixed in this
pass: batching those five per-ticker reads the same way News was batched
would need new multi-ticker read methods on `SupplementalResearchService`/
`AnalystEventsService` and a corresponding `ResearchService` entry point,
a larger, more invasive change than this diagnosis called for -- worth
revisiting if it becomes the actual bottleneck at real production scale.

**Self-review finding: `get_history_for_tickers` needed to chunk its
`IN (...)` clause.** A second review pass on this diagnosis's own diff
found that the new batched query builds one SQL bind parameter per
ticker with no limit -- fine on this dev environment's SQLite (3.45.1,
tested well past 100,000 bind parameters), but a real risk on any SQLite
build still carrying the pre-3.32.0 default `SQLITE_MAX_VARIABLE_NUMBER`
of 999 (some system-linked Python installs never raise it), which would
raise `sqlite3.OperationalError: too many SQL variables` at exactly the
~3,931-ticker scale this diagnosis was triggered by. Fixed by chunking
the query at `_TICKER_CHUNK_SIZE` (500) tickers per round-trip, executed
in a loop and accumulated before grouping by ticker -- concatenating
chunks is safe because each ticker's articles are entirely contained in
one chunk's already-ordered result, so grouping afterward cannot corrupt
per-ticker ordering.

**Validation:** `tests/test_dashboard_full_refresh_banner.py` gained a
regression test seeding `MAX_AUTO_REFRESH_TICKERS + 1` stale tickers,
confirming zero provider calls are made, the cap's warning is shown, and
a later rerun does not retry. `tests/test_news_service.py` gained tests
for `get_history_for_tickers` (matches per-ticker `get_history` exactly,
omits tickers with no articles, respects `as_of`, empty ticker list
returns `{}`, and -- for the chunking fix -- correctly returns complete,
correctly-ordered per-ticker history when the ticker count spans multiple
chunks, including tickers sitting right at a chunk boundary). New
`tests/test_evidence_coverage_universe_breakdown.py` proves the page
actually calls the batched method for the whole universe (not a
per-ticker loop), while the separate Security Detail tab's own
single-ticker `get_history` call is untouched. Full test suite and all
three established smoke tests pass. `git diff --check`: clean.

### 34.1 Follow-up: real incident evidence from the same production run

The same production run surfaced two further, distinct problems while the
automatic refresh above was live -- both diagnosed against real evidence
(a live SQLite lock scenario reproduced locally, and live Yahoo Finance
lookups), not guessed at.

**"database is locked" during the automatic refresh.** The reported error
-- `sqlite3.OperationalError: database is locked` on `UPDATE securities
... WHERE ticker = 'BRK.B'` -- traced to `alpha_lab.database.session.
make_engine` calling bare `create_engine(url)` for SQLite, which leaves
Python's stdlib default: a 5-second busy timeout and the `DELETE` journal
mode. Several write paths (the automatic refresh, the manual Full
Refresh button, batch scripts) can genuinely overlap against the same
database file, and once one write holds the file past 5 seconds, every
other writer fails immediately rather than waiting. Reproduced locally: a
writer holding the lock for 6 seconds against a bare `create_engine`
database reliably raises the identical `OperationalError`; against
`make_engine`'s fix, the same scenario succeeds. Fixed with a 30-second
busy timeout (`connect_args={"timeout": 30}`, the standard SQLAlchemy/
SQLite mitigation) plus `PRAGMA journal_mode=WAL` for file-based
databases (`:memory:` databases skip this -- WAL isn't supported there,
and nothing else can contend with their single in-process connection
anyway). WAL additionally lets readers (e.g. a dashboard page rendering)
proceed without blocking on the one writer.

**Tickers permanently 404ing, forever, every refresh cycle.** The
reported log spam (`HTTP Error 404 ... Quote not found`, `possibly
delisted`) for tickers like `BRK.B`, `AGM.A`, `BF.A`, `AHL$D`, `ALL$B`,
`DBRG$H` is not those tickers actually being delisted -- it's AlphaLab
sending Yahoo Finance a symbol it has never recognized. AlphaLab's
`Security.ticker` follows the universe listing's own share-class
notation (a dot for a share class, e.g. `BRK.B`; a dollar sign for a
preferred-share suffix, e.g. `AHL$D`), but Yahoo's own symbol convention
uses a hyphen instead (`BRK-B`), and a hyphen-plus-`P` for the preferred
form (`AHL-PD`) -- confirmed live against real Yahoo Finance data for
every ticker in the reported log, both directions (the dot/dollar form
404s, the translated form resolves with real quote data). Since this
never succeeds, these tickers stay stale forever and get retried on
*every single* stale-refresh pass -- automatic, manual, and scripted
alike -- permanently wasting cycles and log noise, and permanently
inflating the "stale ticker" count this diagnosis's §34 was originally
about (some fraction of the reported ~3,931 is tickers like these that
can never succeed, not tickers merely waiting their turn). Fixed with
`alpha_lab.providers.yfinance_provider._yahoo_symbol`, applied at the
provider's single `_ticker()` choke point that every method already
shares -- so every one of `YFinanceProvider`'s methods is fixed at once,
without touching each call site. Deliberately scoped to the outbound
Yahoo request only: `Security.ticker` (and every value this provider
itself returns, e.g. `get_company_info`'s own `"ticker"` field) keeps
the original, canonical form -- this is a provider-side translation of
what goes out over the wire, never a rewrite of AlphaLab's own identity
for the ticker.

**Validation:** `tests/test_database.py` gained a fast pragma-value check
(`busy_timeout`/`journal_mode` on a file-based engine, and confirmation
`:memory:` is left alone) plus a real concurrency regression test that
holds a write lock for 6 seconds (longer than SQLite's old 5-second
default, confirmed live to still fail that way) and asserts a second,
concurrent writer succeeds rather than raising. `tests/
test_yfinance_provider.py` gained a parametrized test of `_yahoo_symbol`
covering every notation observed in the incident, plus a test proving
the translated symbol -- not the canonical ticker -- is what actually
reaches `yfinance.Ticker(...)`. Full test suite and all three established
smoke tests pass. `git diff --check`: clean.

### 34.2 External review: one more full-scan finding, one weak test

An external review of this PR confirmed the diagnosis and fixes above,
and raised two further points.

**The main page's own Data Quality table still read every price row.**
`app/dashboard/main.py`'s Data Quality section (unrelated to the
automatic-refresh trigger in §34, but on the same page) selected
`(ticker, date)` for **every** `Price` row in the database, ordered by
ticker/date, and picked each ticker's latest row in Python -- a read that
scales with total price rows (~500/ticker/year) rather than universe
size, executed on every rerun of a page Streamlit re-executes on every
widget interaction. At the reported ~3,931-ticker, ~2-year-history scale
that is on the order of two million rows fetched into Python per render.
Fixed with the same SQL `MAX(date) GROUP BY ticker` pattern `alpha_lab.
refresh.stale_universe_tickers` already established (§30) -- a drop-in
replacement, since the only thing this table ever needed from that read
was each ticker's single latest date.

**A weak test assertion in `tests/
test_evidence_coverage_universe_breakdown.py`.**
`test_universe_breakdown_flat_rows_include_both_tickers` asserted
`tickers_seen or len(rows) > 0`, which passes even if only one ticker (or
neither, so long as some other row exists) actually made it through.
Since `flatten_coverage_rows` always sets `"ticker"` on every row (its own
docstring), the test now asserts the exact set, `tickers_seen ==
{"AAPL", "MSFT"}`, which actually fails if either ticker's rows are
missing.

**Validation:** new `tests/test_dashboard_data_quality.py` seeds several
`Price` rows per ticker, inserted out of date order, and confirms
`latest_by_ticker` reports each ticker's true latest date -- a regression
guard against `GROUP BY`'s correctness, not just its existence. Full test
suite and all three smoke tests pass. `git diff --check`: clean.

## 35. Evidence Coverage Hardening: Investigation (no code change)

Prompted by a direct question -- "which point do we work on hardening
coverage" -- rather than a reported bug. `scripts/coverage_report.py`
against the live 11-ticker universe (3 individual equities -- AAL, MA,
NVDA -- 2 ETFs, 6 macro-proxy indices/commodities) showed two scoring
categories, `analyst_revisions` and `ai_research`, at **0% coverage
across every single ticker**, including MA and NVDA, which are at 100%
on every other category. Both were traced to root cause against the
real database rather than guessed at; neither needed a code change.

**`analyst_revisions`: working as designed, needs elapsed time, not
code.** Real `Estimate` snapshots exist for AAL/MA/NVDA, but only two per
fiscal period, 3 days apart (this environment's estimates were captured
2026-09-14 and 2026-09-17). `alpha_lab.ratings.estimates.
calculate_revision_factors` needs an observation at least 7 days older
than the current one to compute even its shortest window, so every
revision metric correctly returns `None` today. The same "needs real
elapsed time, not more code" shape as §32/§33's own findings -- this
resolves itself as ingestion keeps running day over day.

**`ai_research`: a real, pre-existing capability gap -- not a bug, and
not a case for wiring in the newer AI system.** The screener's
`ai_research` scoring category (`alpha_lab.screener.service`) is fed by
`alpha_lab.ai`'s `AIResearchAnalysis`/`AIResearchService.ensure_all()` --
an "attributable to source documents" design gated by `_ai_is_attributable`
requiring, among other fields, non-empty `analyzed_document_ids`. That
pipeline needs `CompanyDocument` rows (actual filing/press-release text)
to analyze, and `company_documents` has **zero rows in this database**.
Tracing further: `alpha_lab.providers.interfaces.CompanyDocumentProvider`
is declared but has no concrete implementation anywhere in the codebase
-- the existing `SECCompanyFactsProvider` only fetches structured XBRL
numeric facts (revenue, EPS, ...), never narrative filing text. This
feature was scaffolded (interface + consumer service) back in the
project's first PR and never completed with a real document source --
an honest gap the coverage report is correctly surfacing, not a defect
in what exists.

Separately, `alpha_lab.research.ai_rating`'s `AIResearchAssessment` --
the system behind the Company Research page's AI Research Rating --
*does* have real data in this environment (verified: POSITIVE/68.75 for
MA, POSITIVE/75.0 for NVDA, NEUTRAL/43.75 for AAL). It would be tempting
to wire this into the empty `ai_research` scoring category, but its own
module docstring explicitly forbids exactly that: it "interprets
already-validated AlphaLab evidence" and "must not be blended into
[`overall_score`]... or the existing `ai_research` rating category...
which is a different, pre-existing system left completely untouched by
this module." That boundary is deliberate, not an oversight -- it keeps
the objective fundamental score and AI qualitative synthesis
architecturally separate. Blending them to make one coverage number look
better would be exactly the kind of fabricated-looking improvement this
project's evidence-first principle exists to prevent.

**Conclusion, recorded rather than acted on:**

| Finding | Status |
|---|---|
| `analyst_revisions` coverage | Working correctly; needs elapsed time, not code |
| `ai_research` scoring coverage | Known unimplemented capability (no document-ingestion provider exists) |
| `AIResearchAssessment` (AI Research Rating) | Working correctly and independently, by design |
| Coverage report itself | Correctly surfacing a real gap, not miscomputing anything |
| Fundamental score / `ai_research` category boundary | Correct as designed; not touched |

**Next roadmap phase, scoped but not started: Document Evidence Engine
(SEC filing ingestion → legacy `ai_research` scoring).** The narrowest
version that would close this specific gap: SEC EDGAR 10-K/10-Q filing
text ingestion into `CompanyDocument` (CIK resolution, filing/accession
metadata, filed date for PIT, content hash/dedup, source URL), feeding
the `AIResearchService`/`AIResearchAnalysis` pipeline that already exists
and is already wired into scoring -- no scoring-architecture change
needed, only the missing data source. Deliberately out of scope for a
first version: 8-K filings, press releases, arbitrary web scraping, and
any change to `AIResearchAssessment`, Research Stance, or the fundamental
score's own weighting -- each is a separate decision for a later phase,
not bundled into closing this one gap.

**Validation:** none -- no code changed. This section documents a live-
database investigation and its conclusion.

### 35.1 Follow-up: per-ticker sweep against the real dashboard code path

Continued at explicit request ("GDX only has data for momentum, many
market caps are missing") -- a full sweep of every evidence domain for
every real ticker (AAL, MA, NVDA, FTEC, GDX), using `build_security_
coverage_summary` (the exact function the Evidence & Coverage Dashboard
itself calls), not ad hoc queries. Every apparent gap traced to a
verified, correct reason; none needed a code change, and none was
"fixed" by inserting a number.

**Self-correction, recorded rather than hidden.** The first pass of this
sweep queried `current_fund_evidence`'s JSON payload for a top-level
`total_net_assets` key and found it `None` for GDX/FTEC, and reported
that as a bug. It was not one -- `build_fund_evidence` correctly nests
that field under `payload["operations"]["total_net_assets"]`, and it was
there all along: GDX `total_net_assets=23608.55`, `expense_ratio=0.0051`;
FTEC `total_net_assets=691876.75`, `expense_ratio=0.00084` -- both
persisted values matched a fresh live `funds_data.fund_operations` pull
exactly. (Yahoo's reporting units for this specific field were not
independently reconciled against its own separate `info["totalAssets"]`
figure -- the two don't share a clean scale factor for FTEC -- so this
records that the persisted value faithfully matches the source, not a
verified real-world dollar amount; that ambiguity belongs to yfinance's
own API, not to anything AlphaLab computes.) The mistake here was
querying the wrong JSON path, not a defect in the persisted data.
Re-running `scripts/refresh_supplemental_research.py GDX FTEC` (the
correct, legitimate way to refresh this domain) confirmed the same real
values were already present before that run. Recorded here so this
false alarm isn't repeated.

**GDX/FTEC have materially more real coverage than the 8-category
scoring view alone suggests.** That view correctly shows 5 of 8
categories `NOT_APPLICABLE` for an ETF (`business_quality`, `earnings_
growth`, `financial_strength`, `valuation`, `shareholder_return`,
`analyst_revisions`) and only `momentum` populated -- which reads as
"only momentum" if that is the only view consulted. The fuller Evidence
Coverage view (16 rows, not 8) shows real, populated evidence for
Technical (`FULL`, 1.0), Fund Evidence (`FULL`, 1.0), News (`FULL`, 1.0,
20 real articles), Macro Regime (`FULL`, 1.0), and AI Evidence
(`PARTIAL`, 0.87, 2/3 required dimensions assessable) -- five domains
with genuine data the narrower scoring-category view doesn't surface.
Only Analyst Consensus/History/Revisions (`NO_EVIDENCE`/`NOT_COMPUTED`
-- confirmed live: `yf.Ticker("GDX"/"FTEC").get_recommendations()` and
`.get_analyst_price_targets()` both return empty, genuinely no sell-side
analyst coverage exists for either ETF on Yahoo) and the `ai_research`
scoring category (§35's already-documented CompanyDocument gap) are
without evidence -- both real, both already explained, neither a defect.

**AAL's four "missing" valuation/quality metrics are correct, deliberate
refusals to compute a misleading ratio -- verified against live data,
not assumed:**

| Metric | Guard | Verified live |
|---|---|---|
| `roe` (Business Quality) | `_positive(total_equity)` | AAL's `total_equity` = **-$3.97B** (real, confirmed in `fundamentals`) |
| `debt_equity` (Financial Strength) | same negative-equity guard | same |
| `price_fcf` (Valuation) | `_positive(free_cash_flow)` | AAL's latest-quarter FCF = **-$351M** |
| `forward_pe` (Valuation) | `_positive(forward_eps)` | AAL's consensus forward EPS = **-$0.17** (confirmed in `raw_metrics["current_consensus_eps"]`) |

Net income divided by negative equity, or price divided by negative FCF
or negative forward EPS, produces a number that looks like a ratio but
means nothing (a "negative P/E" convention issue well known in equity
research) -- `alpha_lab.ratings.quality`/`alpha_lab.ratings.valuation`
correctly return `None` rather than publish it. AAL's own well-documented
post-2020 balance sheet (heavy debt, negative equity) is the real cause;
there is no missing refresh or provider call that would change this.
`shareholder_return` being `NO_EVIDENCE` for AAL is the same shape:
confirmed live that AAL pays no dividend (`dividendYield`/`dividendRate`
both `None`, `payoutRatio` = 0.0) and has no buyback line in its
quarterly cashflow statement -- genuinely no evidence exists, not a
capture failure.

**Market cap, precisely:** of the 11 tracked tickers, 3 (AAL/MA/NVDA)
have it; the other 8 don't, for two different and both-correct reasons.
6 (`CL=F`, `DX-Y.NYB`, `GC=F`, `^IRX`, `^TNX`, `^VIX`) are futures/
currency-index/rate instruments that structurally have no market cap at
all -- forcing a number here would be fabrication, not hardening. The
remaining 2 (FTEC, GDX) are ETFs, for which Yahoo genuinely never
reports `marketCap` (confirmed live: `None` for both) -- the correct
size analog for a fund is AUM, already captured as `FundEvidence.
total_net_assets`, and (per the self-correction above) was already
present and correct.

**Conclusion:** after tracing every apparent gap in this universe to
verified root cause, none was fixable by more code, a fresh refresh, or
a corrected calculation -- every one is either a genuine absence of real
evidence (backed by a live check, never assumed) or this codebase's own
deliberate refusal to compute a misleading ratio from a genuine negative
input. The two items already on record from §35's first pass
(`analyst_revisions` needing elapsed time; `ai_research` needing the
still-unbuilt Document Evidence Engine) remain the only real, actionable
gaps in this universe.

**Validation:** none -- no code changed. Every claim in this subsection
was checked against either the live database or a live Yahoo Finance
call at investigation time, not assumed from code reading alone.

## 36. Document Evidence Engine (SEC filing ingestion + local AI research)

Closes the `ai_research` scoring gap §35 documented: `CompanyDocumentProvider`
was declared as an interface in this project's very first PR and never
implemented, so `AIResearchService.ensure_all` (which only ever *read*
`CompanyDocument`, never wrote it) had nothing to analyze. This phase
builds both missing halves -- real SEC filing ingestion, and a real
analysis step -- deliberately **without any external LLM API**, per
explicit direction: no OpenAI/Anthropic key is configured in this
environment, and a $0, fully local, fully reproducible design was chosen
over buying one. That reproducibility is itself a real advantage for this
codebase's own point-in-time discipline: "given only documents filed by
date X, what would this classifier have produced" is answerable exactly
and deterministically, which a live LLM call never fully guarantees.

**Ingestion: `alpha_lab.providers.sec_filings.SECFilingDocumentProvider`.**
The `CompanyDocumentProvider` implementation, reusing infrastructure that
already existed rather than duplicating it: `SECClient`'s identity/pacing/
caching (the same class `SECCompanyFactsProvider` already uses for XBRL
facts) and `SECCompanyFactsProvider.company_tickers()`'s existing
ticker->CIK resolution. Fetches `/submissions/CIK{cik}.json`, filters to
`SUPPORTED_FORMS` (10-K/10-K-A/10-Q/10-Q-A, the same scope
`SECCompanyFactsProvider` already uses -- 8-K, press releases, and any
non-SEC source are deliberately out of scope for this first version),
and extracts plain text from each filing's real HTML via a new,
dependency-free `html_to_text` (stdlib `html.parser.HTMLParser` only, per
this project's established preference for not adding a dependency when
the standard library suffices -- see the Signal Predictive-Value phase's
own scipy-avoidance). Bounded at `MAX_DOCUMENT_TEXT_CHARS` (500,000
characters) as a defensive guard against a pathological filing, not a
content judgement. One filing document failing to fetch skips just that
one (mirrors `NewsService.refresh`'s "one item failing never aborts the
batch"), and a ticker with no CIK or no supported filing at all returns
`[]`, never a placeholder.

Verified live against real SEC EDGAR data before writing any test:
CIK resolution for AAL/MA/NVDA, a real filing index (NVDA's 7 most recent
10-K/10-Q filings, correct forms/dates/accession numbers), a real ~2MB
inline-XBRL 10-K fetched and reduced to ~340KB of genuinely readable
plain text (`"...may negatively impact our gross margins and financial
results. Factors that have caused ... to underestimate or overestimate
demand..."` -- real NVDA 10-K language, not a fabricated example).

**Persistence: `alpha_lab.ai.documents.ingest_company_documents`.** The
half `AIResearchService` was always missing. Append-only and
content-hash-deduplicated exactly like `NewsService.refresh` -- fetch
happens entirely before any write, so a failed ingestion leaves prior
documents untouched, and re-ingesting an unchanged filing is a no-op, not
a duplicate row. `CompanyDocument` gained two columns via the established
additive-migration pattern: `retrieved_at` (when AlphaLab itself fetched
the document -- the same PIT-safety shape as every other evidence table's
`retrieved_at`/`ingested_at`, never the filing's own `document_date`,
which a historical read could otherwise leak) and `content_hash` (a
unique index, `WHERE content_hash IS NOT NULL`, mirroring `estimates`'
own `observation_hash` pattern). New `scripts/refresh_company_documents.py`
mirrors `scripts/load_sec_facts.py`'s CLI shape exactly.

**Analysis: `alpha_lab.ai.rule_based.RuleBasedFinancialResearchProvider`
-- the new default.** A deterministic, phrase-lexicon classifier across
all nine `AIResearchResult` score dimensions (guidance, demand, margin
outlook, competitive position, management confidence, balance-sheet
commentary, risk, sentiment, catalyst), extending the same lexicon-based
approach `DeterministicAIResearchProvider` already used as a test fixture
into a real per-dimension design meant for production use. Every excerpt
in `evidence` is a verbatim slice of the real document text (never
generated), tied to the real `document_id` it came from; `key_positives`/
`key_risks` are the literal phrases matched, not a paraphrase. Confidence
is tied to how much real signal was actually found (`total_matches / 10`,
capped at 1.0) rather than merely how many documents were supplied -- a
filing set containing none of these phrases is honestly reported as
zero-confidence, not confidently "neutral" the way `DeterministicAIResearchProvider`'s
own document-count-based confidence would report it.

`configured_ai_research_provider()` now defaults to this provider rather
than "disabled": unlike `OpenAIResearchProvider`, it needs no API key,
makes no external call, and costs nothing to run, so unlike a paid
provider it has no reason to require explicit opt-in.
`ALPHALAB_AI_PROVIDER=disabled` still turns AI research off entirely, and
`ALPHALAB_AI_PROVIDER=openai` still opts into the paid provider -- and,
unchanged from before this phase, still fails closed (`None`) rather than
silently substituting the local provider if `OPENAI_API_KEY` isn't also
set. An explicit request for one provider is never silently satisfied by
a different one.

**Deliberately not done, per explicit scope decision:** no `scikit-learn`
or trained-classifier step (would need a labeled dataset this phase does
not build), no FinBERT or other pretrained model (adds a real, heavy
dependency for a V1 that doesn't need one), no 8-K/press-release
ingestion, and no change to `AIResearchAssessment`/Research Stance/the
fundamental score's own weighting -- each is a separate decision for a
later phase. The staged plan this phase's V1 belongs to (rules -> trained
classifier -> pretrained financial-language model -> calibration against
outcomes) is recorded here for that later phase to pick up, not started
early.

**Real-data validation (live database, real network calls, no
fabrication):** `scripts/refresh_company_documents.py AAL MA NVDA`
stored 45/30/25 real filing documents respectively (AAL back to 2015, MA
to 2019, NVDA to 2020 -- whatever SEC's own "recent filings" index
returns; FTEC/GDX and the macro-proxy tickers correctly yield zero, since
ETFs and indices file different SEC forms, not 10-K/10-Q). Re-running
`scripts/rebuild_research.py` (the same script that already calls
`AIResearchService.ensure_all` via `MarketScreenerService` -- no new
call site needed) then produced real `AIResearchAnalysis` rows: AAL
rated 44.44, MA 38.89, NVDA 36.11, all at confidence 1.0, each citing
both real strengths (`"strong demand"`, `"competitive advantage"`) and
real risks (`"increased competition"`, `"margin decline"`) drawn from
their own actual filings. Re-running `scripts/coverage_report.py`
confirmed the fix directly: `ai_research` coverage for AAL/MA/NVDA moved
from **0.0 to 1.0**, with `_ai_is_attributable` (the exact gate the
screener's scoring category depends on) verified `True` against the real
persisted analysis.

**Validation:** `tests/test_sec_filings.py` (10 tests) -- `html_to_text`
(visible-text extraction, script/style skipped, block-tag breaks, entity
unescaping, the character bound), `SECFilingDocumentProvider` (unresolvable
ticker returns `[]`, form filtering, `since` filtering, one failed fetch
never aborts the batch, real extraction not raw HTML) via a fake SEC
client -- no test talks to real EDGAR. `tests/test_company_document_ingestion.py`
(6 tests) -- new documents stored with PIT fields set, idempotent re-ingestion,
multiple distinct documents, empty-provider-result writes nothing, `since`
passed through, ticker case normalized. `tests/test_rule_based_ai_research.py`
(10 tests) -- determinism for identical input, positive/negative phrase
detection, zero score and zero confidence with no matching phrases,
confidence scaling with real signal density, `risk_score`'s distinct
severity-count polarity, evidence excerpts verified verbatim against the
source text, scores bounded to the schema's range even under repeated-phrase
stress, graceful handling of zero documents and documents without an id.
`tests/test_ai_search_phase3.py` gained four tests for
`configured_ai_research_provider`'s selection logic (rule-based default,
`disabled`, `openai` with a key, `openai` without one never silently
falling back). Full test suite and all three established smoke tests
pass -- the Phase 3 smoke test's own rating fluctuation between runs was
independently confirmed unrelated to this phase (it uses its own explicit
`DeterministicAIResearchProvider` fixture, untouched here; the drift
traces to that smoke test's own `date.today()`-relative `Estimate`
fixtures shifting real analyst-revision windows day to day, the same
"needs real elapsed time" shape §35 already documented elsewhere).
`git diff --check`: clean.

### 36.1 Self-review findings, fixed before merge

A `/code-review --diff high` pass against this phase's own diff (per
explicit request: "bug check") found six real issues, all verified
against actual code or live data before fixing, none requiring a design
change:

1. **A single unsafe excerpt silently zeroed an entire ticker's
   analysis.** `EvidenceReference`'s own `no_price_target` validator
   raises `ValueError` if an excerpt contains "price target"/"target
   price" (a real 10-K/10-Q risk-factor section discussing analyst price
   targets near an otherwise-real lexicon match), and
   `analyze_documents` catches *any* exception from the whole `analyze()`
   call and returns `None` for the whole result -- reproducing the exact
   0% `ai_research` coverage this phase exists to fix, for any ticker
   unlucky enough to have one such excerpt. Confirmed by directly
   constructing the crash-triggering `EvidenceReference` and observing
   the real `ValidationError`. Fixed in `RuleBasedFinancialResearchProvider._to_evidence`:
   the `try/except ValueError` now wraps each individual excerpt, so one
   unsafe excerpt is dropped from `evidence` while the phrase match still
   counts toward the score (`test_analyze_survives_an_excerpt_that_would_trip_the_price_target_validator`).
2. **The filing index was permanently cached after its first fetch.**
   `SECFilingDocumentProvider.get_documents` called `client.get_json`
   with the default `refresh=False` for `/submissions/CIK{cik}.json` --
   correct for an individual filing document (immutable once filed) but
   wrong for the index itself, which is exactly what tells a re-run
   about filings made since the last one. Fixed by passing
   `refresh=True` for that one call
   (`test_get_documents_refreshes_the_submissions_index_every_call`).
3. **Older filings were silently truncated.** `filings.recent` in SEC's
   submissions JSON is capped at roughly the most recent ~1,000 filings
   across *every* form type combined; a long-lived filer's older 10-Ks/
   10-Qs live instead in paginated `filings.files` entries, fetchable at
   `/submissions/{name}`. Confirmed live: NVDA has a paginated file
   covering 1998-03-06 to 2020-08-17 (1,484 filings), MA one covering
   2001-06-06 to 2019-03-02 (1,379 filings) -- matching this phase's own
   earlier real-data validation's observed cutoffs exactly. Fixed by
   adding `_filing_rows()` and merging every `filings.files` page into
   the row set before filtering to `SUPPORTED_FORMS`
   (`test_get_documents_merges_paginated_filing_history`).
4. **No caching on the filing-document HTML fetch**, so every re-run
   re-downloaded and re-parsed every already-ingested filing, not just
   new ones. Fixed as a direct consequence of finding 6 below: routing
   the fetch through `SECClient.get_text`, which shares `get_json`'s
   disk-cache pattern, made previously-fetched filing text free on
   subsequent runs.
5. **N+1 dedup queries.** `ingest_company_documents` issued one
   `SELECT` per candidate document to check `content_hash` membership,
   so a ticker with dozens of already-ingested filings paid one round
   trip per filing on every re-run just to discover it had nothing new.
   Fixed by batching into a single
   `SELECT content_hash WHERE content_hash IN (...)` before the loop,
   with an in-loop `existing_hashes.add(...)` guard against duplicate
   text within the same batch (no chunking needed: realistic per-ticker
   filing counts are well under SQLite's ~999 bind-parameter limit,
   unlike the ticker-universe case elsewhere in this codebase that does
   need it). `test_ingest_deduplicates_identical_text_within_the_same_batch`
   and `test_ingest_stores_only_the_new_documents_in_a_mixed_batch` cover,
   respectively, the within-batch guard and the across-runs lookup.
6. **Encapsulation violation.** The original filing-document fetch
   reached into `SECClient`'s private `_last_request` attribute from
   outside the class to implement its own second HTTP fetch path
   instead of reusing `SECClient`'s shared identity/pacing/retry
   handling. Fixed by adding a proper public `SECClient.get_text(url,
   *, refresh=False) -> str | None` method (same disk-cache/pacing/retry
   shape as `get_json`, returns `None` rather than raising on total
   failure) and removing the provider's own fetch method entirely
   (`tests/test_sec_edgar_client.py`, 4 tests;
   `test_get_documents_uses_get_text_not_a_second_http_client`).

`tests/test_sec_filings.py` was fully rewritten (13 tests, up from 10)
to mock `get_json`/`get_text` instead of a since-removed method, and
gained the three regression tests above (findings 2, 3, 6).
`tests/test_company_document_ingestion.py` grew to 8 tests (finding 5).
`tests/test_rule_based_ai_research.py` grew to 11 tests (finding 1).
Full test suite, all three smoke tests, and `git diff --check` all
re-run clean after these fixes (one unrelated pre-existing failure in
`tests/test_dependency_lock.py`, confirmed via `git stash` to fail
identically without this phase's changes -- installed package versions
in this environment have drifted from `requirements.lock`, unrelated to
this phase).

## 37. Document ingestion hardening + rule-based AI research calibration

Explicit direction after §36: harden the Document Evidence Engine's real
ingestion path further, then calibrate `RuleBasedFinancialResearchProvider`'s
own scores against real forward returns rather than trusting a phrase
lexicon's design intent alone. Branched directly off §36's PR rather than
off `main`, so this phase never needs a later rebase across it.

### 37.1 Ingestion hardening

Three real robustness gaps, all found by reading the ingestion code with
an adversarial eye and confirmed against live behavior, none hypothetical:

1. **A single failed paginated filing-index page aborted the whole
   ticker.** `SECFilingDocumentProvider.get_documents` already treats one
   failed *filing document* fetch as skip-just-that-one (`get_text`
   returns `None`, never raises) -- but a failed *page* fetch
   (`get_json` for a `filings.files` entry) still propagated straight
   out, discarding the "recent" filings already collected too. Fixed:
   each page fetch is now wrapped in `try/except (RuntimeError,
   ValueError)` and skipped on failure, exactly mirroring the existing
   per-document rule -- the most recent filings are the ones that matter
   most, and one unreachable older-history page must never cost a ticker
   its recent filings as well.
2. **`_filing_rows` silently misaligned mismatched-length arrays.** Its
   `zip(forms, filed_dates, accessions, primary_documents)` had no
   `strict=True`, so a malformed SEC payload (arrays of different
   lengths) would have quietly zipped a form with the wrong filing date,
   accession, or document -- reporting a real filing under fabricated
   metadata, which is worse than failing loudly. Now raises `ValueError`
   on a length mismatch, caught per-page by fix 1 above so a malformed
   page still can't take down the rest of the ticker's real filings.
3. **SEC EDGAR uses different ticker notation than AlphaLab's own
   canonical tickers -- confirmed live.** `SECCompanyFactsProvider.
   company_tickers()` keys tickers exactly as SEC's `company_tickers.
   json` spells them: `"BRK-B"`, not AlphaLab's canonical `"BRK.B"`. A
   plain `.get(ticker)` therefore silently returned `None` --
   indistinguishable from "not a real company" -- for every dual-class or
   preferred-share ticker, exactly the same mismatch already fixed for
   Yahoo Finance earlier this session (`yfinance_provider._yahoo_symbol`).
   Verified live: `provider.get_documents("BRK.B")` returned 0 documents
   before the fix, 112 real Berkshire Hathaway 10-K/10-Q filings after
   it; `scripts/load_sec_facts.py BRK.B` (the pre-existing SEC facts
   pipeline, which has carried this exact bug since before this session)
   went from "SEC CIK unavailable" to 1,379 real facts and 273 filing
   snapshots. Fixed by extracting the translation into a new shared
   `alpha_lab.providers.ticker_notation.to_hyphenated_symbol` (both
   providers were confirmed to need the identical dot->hyphen,
   `$X`->`-PX` mapping) and adding `SECCompanyFactsProvider.resolve_cik`,
   now used by both `SECFilingDocumentProvider.get_documents` and
   `scripts/load_sec_facts.py` in place of a raw dict lookup.
   `yfinance_provider._yahoo_symbol` now delegates to the same shared
   function rather than duplicating it.

New `tests/test_ticker_notation.py` (6 cases) and a
`test_get_documents_resolves_a_dotted_share_class_ticker` regression test
in `tests/test_sec_filings.py`, which also gained
`test_get_documents_survives_a_failed_paginated_page_without_losing_recent_filings`,
`test_get_documents_survives_one_malformed_page_and_keeps_the_others`, and
`test_filing_rows_raises_loudly_on_mismatched_array_lengths` for findings
1 and 2.

### 37.2 Calibration: does the rule-based signal predict anything?

`alpha_lab.analytics.signal_predictive_value` gained
`collect_rule_based_ai_research_observations`/
`correlate_rule_based_ai_research_with_forward_returns`, extending the
module's existing signal/forward-return correlation study (§33 Signal
Predictive-Value Study) to the Document Evidence Engine's own output --
still read-only, never wired into scoring or backtesting.

Unlike Analyst Consensus/AI Research Rating (gated on `ResearchSnapshot`
accumulation, near-zero today), this domain is testable immediately for
a structural reason: its own historical record is each ticker's real SEC
filing history (`CompanyDocument.document_date`), which already spans
years, not an accumulating snapshot count -- the same "history that
already exists regardless of what ran today" shape Technical Summary has
via `Price`. At each real filing date, the study reconstructs, PIT-safe,
exactly what `RuleBasedFinancialResearchProvider` would have scored using
only documents filed on or before that date (never the persisted
`AIResearchAnalysis` row, which stores only the latest full-history
analysis), and correlates `AIResearchResult.ai_rating` -- the same 0..100
composite the `ai_research` scoring category is actually built from --
against the ticker's own real forward return.

**A real PIT bug found and fixed while building this, affecting
pre-existing code too.** The first live run produced several NVDA
observations with an *identical* forward return regardless of filing
date. Root cause: NVDA's real SEC filing history starts 2020-08-19, but
its stored `Price` history only starts 2021-09-15 (a live, environment-
specific fact, not a code bug in isolation) -- `prices.index.searchsorted
(as_of, side="right")` silently resolves any `as_of` before the whole
Price series to position 0, so every filing date before 2021-09-15 was
being paired with that same single 2021-09-15 price as if it were "the
first trading day after" a filing from over a year earlier. This exact
`searchsorted` call, with the exact same blind spot, already existed in
`_collect_snapshot_domain_observations` (used by Analyst Consensus/AI
Research Rating) -- it had simply never been exercised by a real `as_of`
old enough to trigger it. Fixed once, shared: a new
`_forward_return_from(prices, as_of, forward_days)` helper adds a
`_MAX_NEXT_TRADING_DAY_GAP_DAYS = 10` bound -- if the resolved position's
own date is more than 10 calendar days after `as_of` (a real weekend or
holiday cluster is a few days; anything past that means Price history
simply doesn't cover this era yet), the pairing is skipped as untestable
rather than silently misattributed. Both `_collect_snapshot_domain_observations`
and the new `collect_rule_based_ai_research_observations` now share this
one helper. Regression tests: `test_forward_return_from_skips_when_as_of_predates_all_stored_price_history`,
`test_forward_return_from_computes_correctly_for_a_normal_gap`,
`test_forward_return_from_none_when_not_enough_future_history`.

**Real result, reported plainly (not spun toward a predetermined
conclusion):** `scripts/analyze_signal_predictive_value.py AAL MA NVDA
--forward-days 20` against the real, live-ingested filing history (45/
30/25 documents respectively) and real `Price` history produced 59 real
observations: **pearson +0.065, spearman +0.058** -- both near zero. At
this sample size and horizon, `RuleBasedFinancialResearchProvider`'s
`ai_rating` shows no meaningful correlation with subsequent 20-trading-
day returns for these three tickers. This is an honest, expected result
for a simple phrase-lexicon heuristic over a 3-ticker sample, not a
failure to "make the calibration work" -- exactly the module's own
existing "never spin toward a significance claim the sample doesn't
support" convention (see its docstring).

**A secondary, real observation worth recording for future work, not
fixed in this pass:** `ai_rating` trended upward for NVDA across the
sampled dates (27.78 in 2021 to 36.11 in 2026) as more filings
accumulated. Each per-dimension score is `max(-2, min(2, positive_count -
negative_count))` over the *entire* cumulative document set -- so as
real filing history grows, a dimension with a persistent net-positive
tilt (ordinary corporate boilerplate skews positive far more often than
negative) tends toward the +2 cap and stays there, regardless of what
the most recent filing actually says. This is a real characteristic of
the production system today (`AIResearchService.ensure_all` also scores
against the full cumulative document set, not a rolling window) -- not
unique to this calibration study, and not addressed here; a future phase
could window the analysis (e.g. trailing N filings) if the cumulative
scoring turns out to matter for calibration quality once more tickers
and more history are available for a larger-sample re-run.

New tests in `tests/test_signal_predictive_value.py` (18 total, up from
12): the three `_forward_return_from` tests above, plus
`test_collect_rule_based_ai_research_observations_pairs_cumulative_documents_with_forward_return`,
`test_collect_rule_based_ai_research_observations_skips_tickers_with_no_documents`,
`test_collect_rule_based_ai_research_observations_skips_tickers_with_no_prices`.

**Validation:** full test suite, all three smoke tests, and `git diff
--check` all clean (the same unrelated, pre-existing `test_dependency_lock.py`
environment-drift failure as §36.1, reconfirmed unrelated). Real-data
validation used live SEC EDGAR and Yahoo/live `Price` data already in
this environment's database; no fixture or synthetic data anywhere in
this phase's own findings.
