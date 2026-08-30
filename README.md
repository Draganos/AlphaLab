# AlphaLab

AlphaLab is a local, transparent quantitative investment-research application. Phase 1 downloads real US market and company data through a replaceable provider, stores it in SQLite, calculates independent price/fundamental factors, produces configurable 0–100 scores, and exposes the results in a Streamlit screener.

> **Research and paper trading only.** AlphaLab does not connect to a broker, execute real-money trades, predict prices with an LLM, or promise market outperformance. Its central question is: **“Is AlphaLab actually outperforming after adjusting for risk and trading costs?”** A valid eventual conclusion is that AlphaLab has **not** demonstrated an edge.

## Phase status

- **Phase 1.5** provides the reproducible ingestion, database, factor, scoring, and offline-fixture foundation.
- **Phase 2** provides historical point-in-time backtesting, constrained portfolio construction, passive/manual/systematic comparisons, and expanding walk-forward validation. It does not backfill current Phase 3 evidence into history.
- **Phase 3** provides broad market discovery, Sharia-preferred research filtering, prospectively timestamped estimate revisions, optional attributable AI document analysis, themes, saved screens, and company research.
- **Phase 3.1** hardens provider capabilities and field priority, adds the SEC Companyfacts vertical, persists current-only research snapshots, strengthens evidence coverage, and makes Streamlit filtering/sorting read-oriented.

Remaining future work includes validated historical universe membership, UAE ingestion, brokerage execution, leverage, shorts, options, ML prediction, and parameter mining. These are not presented as implemented.

## Architecture

```text
alpha_lab/
  config/          typed YAML/environment configuration
  database/        SQLAlchemy schema and transaction lifecycle
  factors/         presentation-independent raw and percentile factors
  ingestion/       idempotent provider-to-database service
  providers/       abstract contract and optional yfinance adapter
  strategy/        transparent composite score and contributions
  utils/           structured logging
app/dashboard/     Streamlit screener and data-quality display
alpha_lab/backtest/ next-open simulator, costs, benchmark, database runner
alpha_lab/portfolio/eligibility and equal/score/inverse-volatility weights
alpha_lab/analytics/finite-safe performance measures
alpha_lab/validation/expanding walk-forward folds
alpha_lab/experiments/passive/manual/systematic comparisons
config/            editable strategy, universe, risk, and weight settings
scripts/           schema initialization and real-data ingestion
tests/             focused unit/integration tests
```

Missing values stay `NULL`/`NaN`. Composite scoring reports its data coverage and renormalizes only across available categories, so absence is not silently treated as either strength or weakness. Missing, stale, and provider-unsupported states are distinct, explicit states. Scores carry a score-engine version, canonical configuration hash, and evaluation date so identical inputs and configuration reproduce the same result.

## Requirements and installation

- Python 3.12+
- Network access is needed only when downloading data with yfinance.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
cp .env.example .env
python scripts/init_db.py
```

`pyproject.toml` and `requirements.txt` retain the direct dependency declarations. `requirements.lock` pins the complete validated Python 3.12 runtime and test dependency graph; install it first and then install AlphaLab editable with `--no-deps` as shown above. Regenerate the lock intentionally whenever direct dependencies change. Exact package versions improve reproducibility, but AlphaLab does not claim bit-identical floating-point output across operating systems, CPU architectures, or BLAS implementations; numerical tests use tolerances where appropriate.

The project retains a pip-compatible lock rather than `uv.lock` because its established installation/CI interface is `requirements.txt`/pip and registry metadata was unavailable while preparing this clean integration. The lock is checked against the installed transitive graph in the offline suite; a future intentional packaging migration may replace it with a generated `uv.lock`.

## Verify installation (offline deterministic workflow)

After dependencies have been installed, this exact workflow requires no market-data API or internet access:

```bash
python scripts/init_db.py
python scripts/smoke_test.py
python scripts/smoke_test_phase2.py
python scripts/smoke_test_phase3.py
pytest
streamlit run app/dashboard/main.py --server.headless true
```

The smoke test creates a temporary SQLite database, loads data clearly identified as a deterministic synthetic fixture, runs the same ingestion, factor, and composite-scoring path, asserts usable output, and deletes the database. Synthetic observations use the `synthetic-fixture-v1` provider provenance and must never be interpreted as actual securities or market history. Stop the final dashboard command with `Ctrl-C` after its health check succeeds.

All important defaults live in `config/default.yaml`. Set `ALPHALAB_CONFIG` to use another YAML file or `ALPHALAB_DATABASE_URL` to override the database. Offline verification requires no secrets. `OPENAI_API_KEY` enables the already implemented optional Phase 3 AI analyst; without it, AI evidence remains unavailable and the application continues normally.

## Load real US data

Load the configured NVDA, MA, AAL, FTEC, and GDX universe:

```bash
python scripts/load_us_data.py
```

Or add arbitrary US tickers and select the history length:

```bash
python scripts/load_us_data.py MSFT COST --years 10
```

The provider returns unavailable fields as null. In particular, yfinance does not supply trustworthy point-in-time analyst-estimate history, and AlphaLab does not manufacture it. Provider/API failures are logged and fail the affected command visibly.

## Start the dashboard

```bash
streamlit run app/dashboard/main.py
```

The dashboard labels the core portfolio separately from the systematic experimental sleeve. The AED 5,000 value is a paper-simulation setting, **not** a recommendation to invest that amount. An empty database produces instructions rather than fabricated sample prices.

## Run tests

```bash
pytest
```

The Phase 1 suite covers database CRUD, idempotent ingestion, unknown publication dates, momentum and fundamental calculations, missing-data propagation, percentile ranking, scoring contributions, coverage, and configuration validation.
The core suite uses in-memory or temporary SQLite databases and deterministic local CSV fixtures. It does not call yfinance or require network access; external-provider behavior is exercised through injected fakes.

## Phase 2 backtesting

Run a database-backed research backtest after loading the desired symbols and benchmark:

```bash
python scripts/load_us_data.py SPY NVDA MA AAL FTEC GDX --years 10
python scripts/run_backtest.py --start 2023-01-01 --end 2025-12-31 --benchmark SPY --weighting equal
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
Copy-Item .env.example .env
python scripts/init_db.py
python scripts/smoke_test.py
python scripts/smoke_test_phase2.py
pytest -v
streamlit run app/dashboard/main.py
```

### Assumptions and controls

- **No same-close execution:** a signal generated after the close on date T executes at the next available trading session's open. Weekends and missing dates are handled by the observed price calendar.
- Prices and momentum are truncated at T. Fundamentals come from the append-only as-of query and require `publication_date <= T`; unknown dates remain unavailable. Estimates are not scored, and the estimate query permits only `observation_date <= T`.
- `strategy.minimum_data_coverage` defaults to 70%. A raw score remains auditable below that threshold but is ineligible for systematic selection. Every omission carries an explicit reason.
- Because Phase 2 deliberately does not fabricate revision, valuation, dividend, or AI histories, the currently implemented factor categories may provide less than 70% coverage for real securities. The honest default outcome can therefore be cash until richer point-in-time data exists; researchers may lower the threshold explicitly, but the lower-evidence status remains visible.
- Portfolios are long-only, unlevered, and may remain partly or entirely in cash. Equal, score, and inverse-historical-volatility weighting respect position, sector, score, coverage, and portfolio-count constraints.
- Costs include one fixed and percentage commission plus a single adverse execution-price adjustment composed of half the assumed spread and slippage. Passive and manual initial purchases use the same cost model as AlphaLab.
- Daily NAV is cash plus holdings marked to the latest available close. The audit result contains rebalance candidates, exclusions, scores, coverage, targets, trades, costs, and cash history.
- Performance analytics return `None`, not infinity, when volatility, downside, tracking error, duration, or benchmark overlap is insufficient.

### Walk-forward validation

`alpha_lab.validation` creates expanding, non-overlapping calibration/test folds and reports in-sample and out-of-sample metrics independently. It does not optimize thousands of combinations. An OOS/IS Sharpe ratio below 0.5—or an undefined comparison—reports **“No repeatable edge demonstrated.”**

### Critical limitations

> **SURVIVORSHIP BIAS RISK:** `data/universes/us_research_sample.csv` is a configurable present-day-style research list, not historical index membership. AlphaLab displays this limitation and does not claim to correct it.

Phase 2 does not apply `membership_start` or `membership_end` columns during scoring. Those columns do not convert a CSV into validated historical membership data; enforcing dated membership remains future work.

yfinance does not provide reliable point-in-time publication history for all fundamentals. Such records remain unavailable, so real-data Phase 2 tests may be mostly momentum-driven. Current estimate snapshots are never reconstructed into fictional revision histories. Results remain research/paper simulations, not investment advice or evidence of a repeatable edge.

## Phase 1.5 reliability fixes

- Runtime and test dependency ranges were ambiguous; direct dependencies are now exact-version pinned.
- There was no offline end-to-end path; committed synthetic price/fundamental CSV fixtures and `scripts/smoke_test.py` now provide one with unmistakable fixture provenance.
- Observations lacked ingestion provenance; prices, fundamentals, and estimates now carry provider, source, currency, and ingestion-time metadata.
- Scores could not identify their implementation/configuration; composite results and stored factor scores now carry a stable version and canonical SHA-256 configuration hash.
- Factor evaluation had no as-of boundary; an explicit evaluation date now filters prices and filters fundamentals by publication date, never by fiscal period alone.
- Missing values were implicit and NaN could enter scoring; missing, stale, and unsupported states are distinct, and non-finite factor inputs are excluded explicitly.
- Configuration validation checked only the total weight; it now rejects missing/extra categories, negative or non-finite weights, and invalid position ranges.
- SQLite tests created sessions ad hoc; a rollback-isolated session fixture and idempotent schema test now protect test independence.
- The existing additive schema initializer could not upgrade Phase 1 databases; it now adds Phase 1.5 metadata columns without rewriting existing records.
- Fundamental revisions are append-only and exactly idempotent: an observation hash prevents duplicate versions, while point-in-time queries select the newest publication available for each fiscal period as of the requested date.
- Coverage confidence thresholds are configurable. Below 40% coverage the label is `Insufficient data`; from 40% to below 70% it is provisional; at 70% and above the normal interpretation is shown. The raw numerical score and raw interpretation remain available for audit.

## Database and point-in-time discipline

Run `python scripts/init_db.py` whenever setting up a database. Initialization is additive and simple for this first local release. Financial records have both a fiscal `period` and nullable `publication_date`; later historical engines must filter on publication date and must never infer that a fiscal-period end means information was public. Estimate records carry an `observation_date` for the same reason. Production upgrades that alter existing columns should use a versioned migration tool such as Alembic.

## UAE data

The configured UAE universe is EMAAR, ALEC, DUBAIRESI, PARKIN, AIRARABIA, ALDAR, ADNOCGAS, EAND, SALIK, and DU. Reliable UAE CSV importing and validated templates belong to Phase 4. It has not been falsely presented as complete in Phase 1; do not place unvalidated downloads directly into the database. The future adapter will use the same provider/database boundary and preserve unknown values.

## Factor and score caveats

- Percentile ranks are relative to the loaded universe. Very small universes make ranks unstable.
- Fundamentals supplied by yfinance can be sparse, restated, or have unknown publication dates. They are suitable for current screening, not yet for historical simulation.
- ETFs may not have company-style financial statements. Their related factors correctly remain unavailable.
- The legacy Phase 1 screener does not retroactively gain Phase 3 evidence. The Phase 3 Market Screener implements current valuation, shareholder-return, prospective estimate-revision, and optional attributable AI categories; missing provider evidence remains visibly unavailable.
- Renormalized scores with low data coverage should not be compared as if they had complete evidence. Always inspect the coverage and breakdown.
- Market data may be delayed, adjusted, incomplete, or changed by its upstream provider. AlphaLab stores what the provider returned and never fills a missing observation with invented data.

## Interpretation of backtests

Phase 2 reports transaction-cost-aware, point-in-time portfolio simulations, passive benchmarks, and expanding-window walk-forward results. These are research measurements rather than alpha claims. Every backtest must use only information whose observation/publication date is on or before the simulated decision time, keep in-sample and out-of-sample performance separate, account for spread/slippage/commission, and disclose survivorship limitations.

Historical performance—even when correctly computed—is not evidence that returns will persist. Data mining, regime changes, survivorship bias, revisions, implementation costs, and parameter instability can erase apparent outperformance. AlphaLab is deliberately designed to surface these limitations rather than optimize until a compelling chart appears.

## Logging and safety

Commands use structured JSON logging for lifecycle events and errors. API keys must be supplied only through environment variables and never committed. There is no leverage, margin, options, short selling, automatic stop-loss selling, or real-money execution in this release.

## Phase 3 market discovery and research

Phase 3 adds a scalable, provider-neutral market-discovery layer without changing Phase 2 backtest execution or accounting. `SecurityUniverseProvider`, `FundamentalDataProvider`, `EstimateProvider`, `CompanyDocumentProvider`, `BusinessClassificationProvider`, `ResearchNewsProvider`, and `AIResearchProvider` keep external integrations replaceable. The included CSV universe provider and deterministic fixtures work offline; optional credentialed providers may be added without becoming a core dependency.

### Sharia-preferred methodology

The versioned policy in `config/ethics.yaml` screens **primary business activity** into `PASS`, `REVIEW`, `EXCLUDED`, or `UNKNOWN`. Conventional banking/interest-based lending and weapon production are deterministic hard exclusions, along with the other enabled policy activities. Payment infrastructure is not treated as banking, and airlines are not excluded merely because they carry debt. Debt and interest exposure are displayed as warning metrics rather than silently becoming a business-activity exclusion. Manual allow/exclude lists are auditable; a manual allow cannot defeat a deterministic hard exclusion.

> **Sharia-preferred screening is a configurable research filter and is not a religious ruling or formal certification.** Users remain responsible for their own investment and values decisions.

When the ethical filter is enabled, portfolio construction defaults to `PASS` only and `EXCLUDED` securities cannot enter regardless of their investment score. `REVIEW`, `EXCLUDED`, and `UNKNOWN` companies remain researchable. Only eligible companies form the live investable percentile/rank reference universe.

### Coverage and rating model

The configurable live model weights Earnings & Growth (15%), Analyst Revisions (15%), Business Quality (15%), Valuation (15%), Momentum (15%), Financial Strength (10%), AI Research (10%), and Shareholder Return (5%). Missing inputs remain unavailable and available categories are transparently renormalized. The UI separately reports overall live, quantitative, AI, and historical point-in-time coverage. Live information is never retroactively admitted into a historical backtest.

Timestamped estimate snapshots are append-only and idempotent. A 7/30/90-day revision exists only where at least two genuine observations support it; AlphaLab never reconstructs history from a current estimate. Valuation and quality helpers reject negative/zero invalid denominators rather than turning missing evidence into zero.

### Pull search, themes, saved screens, and research pages

The **Market Screener** page supports structured filters and a deterministic natural-language interpreter. The interpreter converts recognized phrases into visible `ScreenCriteria`; it never generates a ticker list. Database filtering supplies results, and unsupported requests are labeled **Unsupported / insufficient data**. Evidence-derived theme tags carry source and confidence. Saved screeners persist structured criteria locally and can be saved, loaded through the repository API, renamed, and deleted.

The **Company Research** page shows category scores with raw metrics, coverage, confidence, provenance, the full Sharia-preferred values card, and evidence-bearing AI research. AI is optional and is an analyst layer only: structured Pydantic validation prohibits price-target language, evidence references and prompt/model versions are retained, and provider failure leaves AI missing without crashing screening. Python—not AI—enforces ethical eligibility, ranking, coverage, portfolio rules, and historical point-in-time constraints.

Run all offline verification workflows:

```bash
python scripts/smoke_test.py
python scripts/smoke_test_phase2.py
python scripts/smoke_test_phase3.py
pytest -v
streamlit run app/dashboard/main.py --server.headless true
```

Windows PowerShell:

```powershell
python scripts/smoke_test.py
python scripts/smoke_test_phase2.py
python scripts/smoke_test_phase3.py
pytest -v
streamlit run app/dashboard/main.py --server.headless true
```

### Current provider and coverage limitations

With the bundled provider, live prices and a subset of current company metadata/fundamentals may populate when yfinance and network access are available. Deterministic fixtures populate all smoke-test evidence but are never represented as real market data. Historical categories can populate only from stored prices and fundamentals with real publication dates; estimate revisions require AlphaLab's timestamped snapshots, and AI/document signals are not backfilled into history. Free sources do not consistently provide gross margin, ROIC, buybacks, dividend history, estimate dispersion, or attributable news for every company, so expected live coverage varies and is not promised at 100%. Historical coverage is normally lower. `UNKNOWN` remains `UNKNOWN`.

A configured present-day universe can introduce **SURVIVORSHIP BIAS RISK** in historical research. CSV metadata supports hundreds or thousands of records, but AlphaLab does not claim historically correct index membership unless the supplied data actually establishes it. No paid credentials are required for tests, the application still starts without an AI key, and no Phase 3 feature performs brokerage execution, leverage, shorting, options, ML return prediction, or parameter mining.

### Phase 3 review-correction workflows

The live screener now performs deterministic ethical classification automatically from stored company descriptions, sectors, industries, and metadata provenance. An evaluation is reused only while both its evidence fingerprint and the deterministic `config/ethics.yaml` policy version remain unchanged. Unknown and mixed Financials businesses are `REVIEW`; explicit conventional banking, deposit/lending, consumer/mortgage/specialty-finance, and weapons activity is `EXCLUDED`. Payment networks/processors and attributable passenger-airline businesses remain allowed. Neither an AI result nor a high investment score can override a hard exclusion.

Load a broad credential-free active US symbol universe, then enrich and load whatever current evidence the free provider genuinely returns:

```bash
python scripts/load_universe.py --market US --limit 500
python scripts/load_live_research.py --limit 500 --years 2
```

`load_universe.py` reads and stores the full NASDAQ Trader active non-test, non-ETF NYSE/NASDAQ directory with source provenance. `--limit` applies only to expensive metadata enrichment, not symbol persistence. The enrichment subset alternates deterministically between NYSE and NASDAQ and orders each exchange by genuine stored market cap when available, otherwise ticker; it is exchange-balanced, not claimed to be liquid until price-volume evidence exists. Use `--skip-enrichment` for a symbols-only run. Provider errors remain visible and do not create invented metadata.

The live investable percentile reference applies the configured stale-price, history, and liquidity checks before ranking; `data_quality.live_minimum_average_daily_volume` defaults to 100,000 shares. Securities that fail remain visible for audit but cannot distort valid live percentiles.

For companies with adequate price history and two usable current fundamental periods, the free live workflow can generally populate the Growth, Quality, Valuation, Momentum, and Financial Strength categories—70% of the configured rating weight. Actual coverage varies by issuer and provider response and can be lower. Analyst Revisions require multiple genuine timestamped snapshots; AI Research requires attributable stored documents plus optional configuration; Shareholder Return requires genuine dividend/buyback evidence. None are manufactured to cross the 70% threshold. Fundamentals with unknown publication dates may support the **present-day live** screener but never historical scoring.

Live coverage is metric-complete rather than category-present: each category reports available expected metrics divided by its documented metric set, and overall live coverage is the weighted sum of those per-category fractions. A category score can therefore exist from partial evidence while showing, for example, 43% category coverage; one available metric never makes a category 100% covered.

Live price eligibility counts only finite, positive closes. Analyst revisions require two timestamped observations from one provider and use the nearest current/upcoming forecast period; expired forecasts remain unavailable. When price and shares outstanding produce a current market capitalization, that value is used consistently across valuation, shareholder-return factors, and display, with security metadata serving only as fallback.

Natural-language pull search uses a provider pipeline: user text → deterministic or optional AI interpretation → strict `ScreenCriteria` validation → deterministic database filtering. It supports overall score, coverage, sector, industry, theme, market cap, all eight category scores, debt/EBITDA, and Sharia status. Unsupported conditions are displayed rather than ignored, and the interpreter schema has no field capable of returning a stock list.

Optional providers are disabled by default:

```env
ALPHALAB_QUERY_PROVIDER=deterministic
ALPHALAB_AI_PROVIDER=disabled
# To opt in explicitly:
# ALPHALAB_QUERY_PROVIDER=openai
# ALPHALAB_AI_PROVIDER=openai
# OPENAI_API_KEY=...
```

The live AI provider consumes only stored `CompanyDocument` records, validates the existing strict evidence-bearing schema, rejects evidence IDs not supplied to it, and fails closed to missing AI. It never performs ethical classification or portfolio selection.

## Phase 3.1 provider and current-research hardening

Provider capability is explicit rather than inferred. The four capability states are `RELIABLE_CURRENT`, `RELIABLE_POINT_IN_TIME`, `PARTIAL`, and `UNSUPPORTED`; an unknown provider/field pair is `UNSUPPORTED`. Source priority is field-specific:

| Domain | Preferred source | Fallback / status |
|---|---|---|
| US universe/exchange | NASDAQ Trader | yfinance metadata is normalized and cannot degrade canonical exchange |
| Current OHLCV | yfinance | current/market-date evidence only |
| Reported accounting facts | SEC Companyfacts | yfinance is current-only `PARTIAL` fallback by field |
| Estimate history | AlphaLab prospective snapshots | current estimates never reconstruct history |
| Official filing facts | SEC Companyfacts | filing documents themselves remain unsupported in this vertical |
| AI documents | Stored attributable filing/IR documents | optional AI summarizes; it never establishes facts or eligibility |

The SEC vertical uses official Companyfacts JSON with an honest configurable User-Agent, pacing, bounded retries, local response caching, explicit US-GAAP concept precedence, compatible units, accession numbers, filing dates, fiscal metadata, and append-only raw facts plus filing-version fundamentals. Supported mappings are revenue, gross profit, operating income, net income, diluted EPS, cash, explicitly reported total long-term debt/finance-lease obligations, equity, assets/current assets/current liabilities, dividends paid, and common-stock repurchases. EBITDA, free cash flow, ROIC, issuer-extension concepts, ambiguous debt aggregation, and conflicting units are deliberately unavailable rather than inferred.

```bash
export ALPHALAB_SEC_USER_AGENT="AlphaLab Research you@example.com"
python scripts/load_sec_facts.py NVDA MA AAL
python scripts/rebuild_research.py
python scripts/coverage_report.py
```

Data refresh, research rebuild, and page rendering are separate operations. The Market Screener and Company Research pages only read the latest immutable `CurrentResearchBuild` snapshot. They do not call networks, AI, ethical persistence, theme derivation, historical scoring, or whole-universe rebuilding on widget reruns. The legacy home page remains restricted to `universe.us` and uses a 900-second cache. A current snapshot is never queried by the historical scoring/backtest service.

Category coverage remains metric-level and metric weights are declared explicitly in the live scoring service (currently equal within each documented metric set). Earnings Growth requires at least one genuine growth metric; Business Quality requires at least three usable expected metrics; Valuation and Financial Strength at least two; Momentum requires at least three including 3-month return; Revisions require at least one actual revision metric; and Shareholder Return requires at least one genuine component. AI requires attributable documents/evidence separately. `coverage_report.py` reports every ticker, per-category coverage, unavailable categories, median, p25, p75, minimum, maximum, and category availability without imputing missing evidence.
