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
