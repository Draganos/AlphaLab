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
- **CalibrationAlignment** — a future evidence layer connecting Donatien's
  sector/tier weights to AlphaLab securities via canonical sector/GICS
  mapping. Not implemented; AlphaLab currently has no controlled GICS
  taxonomy (`Security.sector`/`.industry` are free-text passthrough from
  yfinance).
- **NewsImpact** — a future classification of news events against existing
  theses/calibration. Not implemented.
- **Conviction Layer** — a future cross-domain agreement/conflict
  assessment. Not implemented, and per the project brief must not become a
  simple average when it is.
- **Signal Conflict Detection** — not implemented.
- **Ranking integration for Donatien/News/Macro** — not implemented, and
  must not be added without empirical backtest validation per the project
  brief.

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

**Not implemented in this step** (see §5): comparison against Donatien's
regime label (§10 of the original brief), official economic data (FRED or
similar), multi-region scopes beyond the `"US"` default, market breadth
(would require the full ingested universe, not a single-ticker proxy — no
honest single-ticker substitute exists), and inflation expectations
(breakeven rates aren't reliably available via yfinance).
