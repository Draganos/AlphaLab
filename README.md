# AlphaLab

AlphaLab is a local, transparent quantitative investment-research application. Phase 1 downloads real US market and company data through a replaceable provider, stores it in SQLite, calculates independent price/fundamental factors, produces configurable 0–100 scores, and exposes the results in a Streamlit screener.

> **Research and paper trading only.** AlphaLab does not connect to a broker, execute real-money trades, predict prices with an LLM, or promise market outperformance. Its central question is: **“Is AlphaLab actually outperforming after adjusting for risk and trading costs?”** A valid eventual conclusion is that AlphaLab has **not** demonstrated an edge.

## Phase status

Phase 1 is a working vertical slice. Later phases—point-in-time backtesting and portfolio construction, estimate revisions and optional AI document analysis, UAE CSV ingestion and paper trading, then constrained experiments—are intentionally not represented by hollow implementations. The schema reserves their auditable records, but no dashboard result claims those capabilities today.

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
pip install -r requirements.txt
cp .env.example .env
python scripts/init_db.py
```

Direct runtime and test dependencies are exact-version pinned in `pyproject.toml` and `requirements.txt`. Teams requiring a fully transitive lock should generate and commit a platform-appropriate lock file in their controlled package environment.

## Verify installation (offline deterministic workflow)

After dependencies have been installed, this exact workflow requires no market-data API or internet access:

```bash
python scripts/init_db.py
python scripts/smoke_test.py
pytest
streamlit run app/dashboard/main.py --server.headless true
```

The smoke test creates a temporary SQLite database, loads data clearly identified as a deterministic synthetic fixture, runs the same ingestion, factor, and composite-scoring path, asserts usable output, and deletes the database. Synthetic observations use the `synthetic-fixture-v1` provider provenance and must never be interpreted as actual securities or market history. Stop the final dashboard command with `Ctrl-C` after its health check succeeds.

All important defaults live in `config/default.yaml`. Set `ALPHALAB_CONFIG` to use another YAML file or `ALPHALAB_DATABASE_URL` to override the database. There are no required secrets in Phase 1; `OPENAI_API_KEY` is merely documented for the optional future AI analyst.

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

## Database and point-in-time discipline

Run `python scripts/init_db.py` whenever setting up a database. Initialization is additive and simple for this first local release. Financial records have both a fiscal `period` and nullable `publication_date`; later historical engines must filter on publication date and must never infer that a fiscal-period end means information was public. Estimate records carry an `observation_date` for the same reason. Production upgrades that alter existing columns should use a versioned migration tool such as Alembic.

## UAE data

The configured UAE universe is EMAAR, ALEC, DUBAIRESI, PARKIN, AIRARABIA, ALDAR, ADNOCGAS, EAND, SALIK, and DU. Reliable UAE CSV importing and validated templates belong to Phase 4. It has not been falsely presented as complete in Phase 1; do not place unvalidated downloads directly into the database. The future adapter will use the same provider/database boundary and preserve unknown values.

## Factor and score caveats

- Percentile ranks are relative to the loaded universe. Very small universes make ranks unstable.
- Fundamentals supplied by yfinance can be sparse, restated, or have unknown publication dates. They are suitable for current screening, not yet for historical simulation.
- ETFs may not have company-style financial statements. Their related factors correctly remain unavailable.
- Phase 1 valuation, dividend, estimate-revision, and AI categories remain visibly unavailable until audited sources and their later engines are implemented.
- Renormalized scores with low data coverage should not be compared as if they had complete evidence. Always inspect the coverage and breakdown.
- Market data may be delayed, adjusted, incomplete, or changed by its upstream provider. AlphaLab stores what the provider returned and never fills a missing observation with invented data.

## Roadmap and backtests

Phase 2 will introduce transaction-cost-aware, point-in-time portfolio simulation, passive benchmarks, and expanding-window walk-forward validation. Until then, AlphaLab makes no backtest or alpha claim. Any later backtest must use only information whose observation/publication date is on or before the simulated decision time, keep in-sample and out-of-sample performance separate, account for spread/slippage/commission, and disclose survivorship limitations.

Historical performance—even when correctly computed—is not evidence that returns will persist. Data mining, regime changes, survivorship bias, revisions, implementation costs, and parameter instability can erase apparent outperformance. AlphaLab is deliberately designed to surface these limitations rather than optimize until a compelling chart appears.

## Logging and safety

Commands use structured JSON logging for lifecycle events and errors. API keys must be supplied only through environment variables and never committed. There is no leverage, margin, options, short selling, automatic stop-loss selling, or real-money execution in this release.
