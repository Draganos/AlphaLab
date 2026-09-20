"""Predictive/decision-value analysis for the supplemental research
signals added since PR #26 -- read-only correlation study between a
signal's historical value and the ticker's own subsequent price return.
Never wired into `HistoricalScoringService`, `alpha_lab.backtest`, or any
scoring/ranking path: this only measures whether a signal *would have*
said anything about what happened next, it does not make that happen.
See ARCHITECTURE.md's "Signal Predictive-Value Study" section for the
phase this belongs to and the scope decision behind it.

Scope, decided explicitly rather than assumed:

- **Technical Summary** is the only domain analyzed by default today,
  because it is the only one *capable* of testable historical depth: it
  is a pure function of `Price` history (`SupplementalResearchService.
  get_technical_summary_as_of`), and `Price` history genuinely accumulates
  day by day regardless of whether anyone ever refreshes anything. Whether
  it *currently has* that depth in a given environment is a separate
  question, discovered empirically while validating this module against
  the live database: a freshly bulk-backfilled environment has every
  `Price` row sharing roughly one real `ingested_at` moment, however far
  back its `date` is (`run_core_refresh` ingests up to two years in one
  call) -- so every sampled historical date still predates that moment,
  and `get_technical_summary_as_of` correctly (not a bug) returns REVIEW
  for all of them, exactly as designed to prevent a leak. Technical
  Summary only becomes genuinely testable for dates on or after whenever
  real, incremental day-by-day ingestion actually began in that
  environment -- the same "needs real elapsed time, not just code" shape
  as the two domains below, just gated by `Price.ingested_at`'s spread
  rather than a snapshot count.
- **Analyst Consensus / AI Research Rating** have no historical depth of
  their own yet: their only history is whatever `ResearchSnapshot` rows
  `ResearchService.snapshot_current_research` has recorded since the
  "Historical Research Reconstruction" phase started calling it -- which,
  the moment that phase shipped, is close to zero snapshots per ticker.
  The methodology for correlating them is fully designed and implemented
  below (`collect_analyst_consensus_observations`/
  `collect_ai_research_assessment_observations`), but each deliberately
  raises `InsufficientSnapshotHistory` rather than silently computing a
  correlation from a handful of near-simultaneous snapshots and reporting
  it as if it meant something. No code change is needed to "enable" them
  later -- they start working correctly the moment enough real history has
  accumulated; see `MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE`.
- **Fund Evidence is excluded by design, not by data depth.** Unlike the
  other three, it has no ordinal rating/score field at all (see
  `alpha_lab.research.fund_evidence.FundEvidence`) -- it is purely
  descriptive (asset allocation, sector weightings, operations, holdings),
  with no single bullish/bearish reading to correlate against a forward
  return. A future phase could design a derived signal from it (e.g.
  concentration or expense-ratio percentile), but that is a genuinely new
  research question, not this module's "does the existing signal predict
  anything" question -- so it is left out entirely rather than forcing an
  artificial score onto it.

Methodology: Pearson and Spearman correlation between a signal's value at
each sampled historical date and the ticker's own actual forward return
starting from that date, using AlphaLab's own stored `Price` history for
the (already-happened, ground-truth) forward return -- no PIT filtering
needed on that side, since it asks "what actually happened next in
reality", not "what did AlphaLab know".

Deliberately reported descriptively only (`pearson`/`spearman`/
`sample_size`), never with a computed "significant" verdict. The standard
`|r| > 2/sqrt(n)` large-sample threshold assumes `n` independent
observations; the Analyst Consensus/AI Research Rating domains sample
every persisted `ResearchSnapshot` for a ticker regardless of how close
together in time they were recorded, so overlapping forward-return
windows from snapshots taken days (or less) apart are not independent of
each other -- treating that raw count as `n` in the threshold would
overstate confidence. Rather than build the clustering-aware correction
this would need (e.g. requiring minimum snapshot spacing, or a
cluster-robust estimator) for what is still a near-zero-history,
exploratory phase, this module reports the correlation and the sample
size it came from and leaves the judgment of whether that is compelling
to whoever reads the result.
"""

from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alpha_lab.config import Settings
from alpha_lab.database.models import Price
from alpha_lab.research.service import ResearchService
from alpha_lab.research.supplemental_service import SupplementalResearchService

# The default threshold for `InsufficientSnapshotHistory` below: Analyst
# Consensus/AI Research Rating raise rather than report a correlation
# computed from fewer real (ticker, snapshot) pairs than this, since for
# them the honest answer below this many observations is "not yet
# testable at all". Technical Summary never raises -- it always reports
# whatever it computed, however small `sample_size` is; see this module's
# own docstring for why no domain reports a computed "significant"
# verdict regardless of `n`.
MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE = 30


class InsufficientSnapshotHistory(RuntimeError):
    """Raised by the Analyst Consensus / AI Research Rating correlation
    studies when fewer than `MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE`
    genuine (ticker, snapshot) pairs exist across the whole universe.
    Expected today: automatic snapshotting only started in the Historical
    Research Reconstruction phase, so there has not been time to
    accumulate real history yet. This is not a bug to work around --
    re-run once more history exists."""


@dataclass
class SignalObservation:
    ticker: str
    as_of: date
    signal_value: float
    forward_return: float


@dataclass
class CorrelationResult:
    signal_name: str
    forward_days: int
    pearson: float | None
    spearman: float | None
    sample_size: int
    observations: list[SignalObservation] = field(repr=False, default_factory=list)


def _price_series(engine: Engine, ticker: str) -> pd.Series:
    """Ascending close-price series straight from `Price`, indexed by
    date -- ground truth for "what actually happened", not a PIT read (see
    module docstring for why the forward-return side needs no `as_of`
    filtering)."""
    normalized = ticker.strip().upper()
    with Session(engine) as session:
        rows = session.execute(
            select(Price.date, Price.close)
            .where(Price.ticker == normalized, Price.close.is_not(None))
            .order_by(Price.date)
        ).all()
    if not rows:
        return pd.Series(dtype=float)
    dates, closes = zip(*rows)
    return pd.Series(list(closes), index=pd.DatetimeIndex(dates).normalize(), dtype=float)


def _correlate(observations: list[SignalObservation], *, forward_days: int, signal_name: str) -> CorrelationResult:
    n = len(observations)
    if n < 3:
        return CorrelationResult(
            signal_name=signal_name, forward_days=forward_days,
            pearson=None, spearman=None, sample_size=n, observations=observations,
        )
    frame = pd.DataFrame(
        {
            "signal": [o.signal_value for o in observations],
            "forward_return": [o.forward_return for o in observations],
        }
    )
    pearson = frame["signal"].corr(frame["forward_return"], method="pearson")
    # Spearman = Pearson correlation of the rank-transformed values --
    # computed this way (not pandas' own method="spearman") because that
    # path transitively requires scipy (verified: crashes without it), and
    # this module's whole point is staying dependency-free like the rest
    # of alpha_lab.analytics; ranking is pure pandas/numpy.
    spearman = frame["signal"].rank().corr(frame["forward_return"].rank(), method="pearson")
    pearson = None if pd.isna(pearson) else float(pearson)
    spearman = None if pd.isna(spearman) else float(spearman)
    return CorrelationResult(
        signal_name=signal_name, forward_days=forward_days,
        pearson=pearson, spearman=spearman, sample_size=n, observations=observations,
    )


def collect_technical_summary_observations(
    engine: Engine,
    tickers: list[str],
    *,
    forward_days: int = 20,
    sample_interval_days: int = 20,
) -> list[SignalObservation]:
    """Walk each ticker's own stored `Price` history in `sample_interval_
    days`-row strides (trading days, not calendar days -- avoids
    weekend/holiday gaps skewing the stride), reconstructing Technical
    Summary exactly as of each sampled date via `get_technical_summary_
    as_of` (PIT-safe: never sees a price bar AlphaLab had not actually
    ingested by that date) and pairing its `overall_score` with the
    ticker's own actual forward-looking return over the next `forward_
    days` trading days. A sample whose reconstructed summary has no
    `overall_score` (REVIEW rating: insufficient indicator coverage at
    that point in its history) is skipped entirely -- correlating a
    REVIEW rating's absence of signal would bias the result toward zero
    for the wrong reason, not a genuine absence of predictive value."""
    supplemental = SupplementalResearchService(engine)
    observations: list[SignalObservation] = []
    for ticker in tickers:
        prices = _price_series(engine, ticker)
        if len(prices) <= forward_days:
            continue
        for i in range(0, len(prices) - forward_days, sample_interval_days):
            as_of = prices.index[i].date()
            current_price = prices.iloc[i]
            future_price = prices.iloc[i + forward_days]
            if current_price <= 0:
                continue
            summary = supplemental.get_technical_summary_as_of(ticker, as_of)
            if summary.overall_score is None:
                continue
            observations.append(
                SignalObservation(
                    ticker=ticker, as_of=as_of,
                    signal_value=summary.overall_score,
                    forward_return=float(future_price / current_price - 1),
                )
            )
    return observations


def correlate_technical_summary_with_forward_returns(
    engine: Engine,
    tickers: list[str],
    *,
    forward_days: int = 20,
    sample_interval_days: int = 20,
) -> CorrelationResult:
    observations = collect_technical_summary_observations(
        engine, tickers, forward_days=forward_days, sample_interval_days=sample_interval_days,
    )
    return _correlate(observations, forward_days=forward_days, signal_name="technical_summary.overall_score")


def _collect_snapshot_domain_observations(
    engine: Engine,
    settings: Settings,
    tickers: list[str],
    *,
    forward_days: int,
    extract_signal_value,
) -> list[SignalObservation]:
    """Shared walk over each ticker's persisted `ResearchSnapshot` history
    (newest first, per `get_research_history`) -- ground truth for when a
    snapshot genuinely existed is each entry's own `created_at`, never its
    `evaluation_date` (see `ResearchSnapshotRepository.get_latest_as_of`'s
    identical rationale). `extract_signal_value(StockResearch) -> float |
    None` isolates the one field each domain differs on; returning `None`
    skips that snapshot (the domain was not computed/available at that
    point) rather than fabricating a value.

    The entry price is the first trading day's close *strictly after*
    `entry.created_at`'s own date, never that same day's close: unlike
    `collect_technical_summary_observations`'s `as_of` (a deliberate
    end-of-day reconstruction boundary), `entry.created_at` is a real,
    uncontrolled intraday timestamp -- a snapshot recorded mid-session
    could otherwise be paired with a same-day closing price that plainly
    was not yet known at that moment.
    """
    service = ResearchService(engine, settings)
    observations: list[SignalObservation] = []
    for ticker in tickers:
        prices = _price_series(engine, ticker)
        if prices.empty:
            continue
        for entry in service.get_research_history(ticker):
            as_of = entry.created_at.date()
            position = prices.index.searchsorted(pd.Timestamp(as_of), side="right")
            if position >= len(prices) or position + forward_days >= len(prices):
                continue
            current_price = prices.iloc[position]
            future_price = prices.iloc[position + forward_days]
            if current_price <= 0:
                continue
            research = service.get_research_snapshot(entry.snapshot_id)
            if research is None:
                continue
            signal_value = extract_signal_value(research)
            if signal_value is None:
                continue
            observations.append(
                SignalObservation(
                    ticker=ticker, as_of=as_of, signal_value=signal_value,
                    forward_return=float(future_price / current_price - 1),
                )
            )
    return observations


def collect_analyst_consensus_observations(
    engine: Engine,
    settings: Settings,
    tickers: list[str],
    *,
    forward_days: int = 20,
    minimum_observations: int = MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE,
) -> list[SignalObservation]:
    """Designed for a future phase, once enough forward-only snapshots
    have accumulated (see module docstring). Uses `AnalystConsensus.
    rating_score` (already numeric, -2..+2) -- no ordinal re-encoding
    needed. Raises `InsufficientSnapshotHistory` below `minimum_
    observations` real (ticker, snapshot) pairs rather than returning a
    misleadingly precise-looking correlation from near-zero history."""
    observations = _collect_snapshot_domain_observations(
        engine, settings, tickers, forward_days=forward_days,
        extract_signal_value=lambda research: (
            research.analyst_consensus.rating_score if research.analyst_consensus is not None else None
        ),
    )
    if len(observations) < minimum_observations:
        raise InsufficientSnapshotHistory(
            f"Only {len(observations)} Analyst Consensus snapshot observation(s) exist "
            f"across the universe (need >= {minimum_observations}). Automatic snapshotting "
            "only started in the Historical Research Reconstruction phase -- re-run this "
            "once more history has accumulated."
        )
    return observations


def collect_ai_research_assessment_observations(
    engine: Engine,
    settings: Settings,
    tickers: list[str],
    *,
    forward_days: int = 20,
    minimum_observations: int = MINIMUM_OBSERVATIONS_FOR_SIGNIFICANCE,
) -> list[SignalObservation]:
    """Same design as `collect_analyst_consensus_observations`, for
    `AIResearchAssessment.score` (already numeric, 0..100)."""
    observations = _collect_snapshot_domain_observations(
        engine, settings, tickers, forward_days=forward_days,
        extract_signal_value=lambda research: (
            research.ai_research_assessment.score if research.ai_research_assessment is not None else None
        ),
    )
    if len(observations) < minimum_observations:
        raise InsufficientSnapshotHistory(
            f"Only {len(observations)} AI Research Rating snapshot observation(s) exist "
            f"across the universe (need >= {minimum_observations}). Automatic snapshotting "
            "only started in the Historical Research Reconstruction phase -- re-run this "
            "once more history has accumulated."
        )
    return observations


def correlate_analyst_consensus_with_forward_returns(
    engine: Engine, settings: Settings, tickers: list[str], *, forward_days: int = 20,
) -> CorrelationResult:
    observations = collect_analyst_consensus_observations(engine, settings, tickers, forward_days=forward_days)
    return _correlate(observations, forward_days=forward_days, signal_name="analyst_consensus.rating_score")


def correlate_ai_research_assessment_with_forward_returns(
    engine: Engine, settings: Settings, tickers: list[str], *, forward_days: int = 20,
) -> CorrelationResult:
    observations = collect_ai_research_assessment_observations(engine, settings, tickers, forward_days=forward_days)
    return _correlate(observations, forward_days=forward_days, signal_name="ai_research_assessment.score")
