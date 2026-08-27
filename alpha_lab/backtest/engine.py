"""Deterministic, long-only, next-session-open portfolio simulator."""

from dataclasses import dataclass
from datetime import date
import math
from collections.abc import Callable
import pandas as pd

from alpha_lab.analytics import performance_metrics
from alpha_lab.config import BacktestSettings
from alpha_lab.portfolio import Candidate, construct_portfolio
from alpha_lab.strategy.historical import HistoricalScore


@dataclass(frozen=True)
class TransactionCostModel:
    fixed_commission: float = 0.0
    percentage_commission: float = 0.0
    spread: float = 0.0
    slippage: float = 0.0
    minimum_trade_amount: float = 0.0

    def execution_price(self, open_price: float, side: str) -> float:
        adjustment = self.spread / 2 + self.slippage
        return open_price * (1 + adjustment if side == "BUY" else 1 - adjustment)

    def commission(self, notional: float) -> float:
        return self.fixed_commission + abs(notional) * self.percentage_commission


@dataclass(frozen=True)
class SimulatedTradeRecord:
    ticker: str
    side: str
    quantity: float
    signal_date: date
    execution_date: date
    execution_price: float
    transaction_cost: float
    pre_trade_weight: float
    post_trade_weight: float


@dataclass(frozen=True)
class RebalanceDecision:
    evaluation_date: date
    execution_date: date
    candidate_universe: tuple[str, ...]
    excluded: dict[str, str]
    scores: dict[str, float | None]
    coverage: dict[str, float]
    selected: tuple[str, ...]
    target_weights: dict[str, float]


@dataclass(frozen=True)
class PortfolioSnapshotRecord:
    snapshot_date: date
    cash: float
    holdings: dict[str, float]
    market_value: float
    total_nav: float


@dataclass
class BacktestResult:
    nav: pd.Series
    cash: pd.Series
    trades: list[SimulatedTradeRecord]
    rebalances: list[RebalanceDecision]
    metrics: dict[str, float | None]
    total_transaction_costs: float
    turnover: float
    holdings: dict[str, float]
    snapshots: list[PortfolioSnapshotRecord]
    limitation: str = "SURVIVORSHIP BIAS RISK: universe membership is not historical unless explicitly supplied."


ScoreFunction = Callable[[date], list[HistoricalScore]]


class BacktestEngine:
    """Signals use data through T and execute only at the next available open."""

    def __init__(self, prices: dict[str, pd.DataFrame], score_function: ScoreFunction,
                 settings: BacktestSettings, *, min_score: float, minimum_coverage: float,
                 min_positions: int, max_positions: int, max_position: float,
                 max_sector: float | None, weighting: str | None = None):
        self.prices = {ticker: _normalize(frame) for ticker, frame in prices.items()}
        self.score_function, self.settings = score_function, settings
        self.min_score, self.minimum_coverage = min_score, minimum_coverage
        self.min_positions, self.max_positions = min_positions, max_positions
        self.max_position, self.max_sector = max_position, max_sector
        self.weighting = weighting or settings.weighting
        self.costs = TransactionCostModel(**settings.costs.model_dump())

    def run(self, start: date, end: date, benchmark: pd.Series | None = None) -> BacktestResult:
        calendar = self._calendar(start, end)
        if len(calendar) < 2:
            raise ValueError("Backtest requires at least two available trading dates")
        signals = set(_rebalance_dates(calendar, self.settings.rebalance))
        holdings: dict[str, float] = {}
        cash = float(self.settings.initial_capital)
        nav_values: dict[pd.Timestamp, float] = {}
        cash_values: dict[pd.Timestamp, float] = {}
        trades: list[SimulatedTradeRecord] = []
        decisions: list[RebalanceDecision] = []
        snapshots: list[PortfolioSnapshotRecord] = []
        pending: tuple[date, dict[str, float], dict[str, str], list[HistoricalScore]] | None = None
        traded_notional = total_costs = 0.0
        for timestamp in calendar:
            current_date = timestamp.date()
            if pending is not None:
                signal_date, targets, construction_excluded, scores = pending
                new_trades, cash, notional, costs = self._execute(
                    signal_date, current_date, targets, holdings, cash)
                trades.extend(new_trades)
                traded_notional += notional
                total_costs += costs
                decisions.append(_decision(signal_date, current_date, targets, construction_excluded, scores))
                pending = None
            nav = cash + sum(quantity * self._price_on(ticker, timestamp, "close")
                             for ticker, quantity in holdings.items())
            nav_values[timestamp] = nav
            cash_values[timestamp] = cash
            snapshots.append(PortfolioSnapshotRecord(current_date, cash, dict(holdings), nav - cash, nav))
            if current_date in signals:
                scores = self.score_function(current_date)
                construction = construct_portfolio([_candidate(score) for score in scores], method=self.weighting,
                    min_score=self.min_score, minimum_coverage=self.minimum_coverage,
                    min_positions=self.min_positions, max_positions=self.max_positions,
                    max_position=self.max_position, max_sector=self.max_sector)
                if timestamp != calendar[-1]:
                    pending = (current_date, construction.weights, construction.excluded, scores)
        nav_series = pd.Series(nav_values, name="strategy_nav")
        cash_series = pd.Series(cash_values, name="cash")
        metrics = performance_metrics(nav_series, benchmark, self.settings.risk_free_rate)
        average_nav = nav_series.mean()
        turnover = traded_notional / average_nav if average_nav > 0 else 0.0
        metrics.update({"turnover": turnover, "total_transaction_costs": total_costs,
                        "cash_allocation_average": float((cash_series / nav_series).mean()),
                        **_trade_statistics(trades)})
        return BacktestResult(nav_series, cash_series, trades, decisions, metrics,
                              total_costs, turnover, holdings, snapshots)

    def _execute(self, signal_date: date, execution_date: date, targets: dict[str, float],
                 holdings: dict[str, float], cash: float) -> tuple[list[SimulatedTradeRecord], float, float, float]:
        timestamp = pd.Timestamp(execution_date)
        raw_prices = {ticker: self._price_on(ticker, timestamp, "open")
                      for ticker in set(holdings) | set(targets) if self._has_price(ticker, timestamp, "open")}
        nav = cash + sum(quantity * raw_prices.get(ticker, 0) for ticker, quantity in holdings.items())
        desired = {ticker: nav * weight / raw_prices[ticker] for ticker, weight in targets.items()
                   if ticker in raw_prices and raw_prices[ticker] > 0}
        orders = [(ticker, desired.get(ticker, 0) - holdings.get(ticker, 0))
                  for ticker in sorted(set(holdings) | set(desired)) if ticker in raw_prices]
        orders.sort(key=lambda item: item[1] > 0)  # sells first
        records: list[SimulatedTradeRecord] = []
        total_notional = total_cost = 0.0
        for ticker, delta in orders:
            if abs(delta * raw_prices[ticker]) < self.costs.minimum_trade_amount:
                continue
            side = "BUY" if delta > 0 else "SELL"
            quantity = abs(delta)
            if not self.settings.fractional_shares:
                quantity = math.floor(quantity)
            if quantity <= 0:
                continue
            execution_price = self.costs.execution_price(raw_prices[ticker], side)
            if side == "BUY":
                max_quantity = max(0.0, (cash - self.costs.fixed_commission) /
                                   (execution_price * (1 + self.costs.percentage_commission)))
                quantity = min(quantity, max_quantity)
                if not self.settings.fractional_shares:
                    quantity = math.floor(quantity)
            notional = quantity * execution_price
            cost = self.costs.commission(notional)
            if quantity <= 0 or notional < self.costs.minimum_trade_amount:
                continue
            pre_nav = cash + sum(q * raw_prices.get(t, 0) for t, q in holdings.items())
            pre_weight = holdings.get(ticker, 0) * raw_prices[ticker] / pre_nav if pre_nav > 0 else 0
            if side == "BUY":
                cash -= notional + cost
                holdings[ticker] = holdings.get(ticker, 0) + quantity
            else:
                cash += notional - cost
                holdings[ticker] = max(0.0, holdings.get(ticker, 0) - quantity)
                if holdings[ticker] < 1e-12:
                    holdings.pop(ticker, None)
            post_nav = cash + sum(q * raw_prices.get(t, 0) for t, q in holdings.items())
            post_weight = holdings.get(ticker, 0) * raw_prices[ticker] / post_nav if post_nav > 0 else 0
            records.append(SimulatedTradeRecord(ticker, side, quantity, signal_date, execution_date,
                                                 execution_price, cost, pre_weight, post_weight))
            total_notional += notional
            total_cost += cost
        if cash < -1e-8:
            raise RuntimeError("Negative cash would violate no-leverage constraint")
        return records, max(cash, 0.0), total_notional, total_cost

    def _calendar(self, start: date, end: date) -> pd.DatetimeIndex:
        dates = set()
        for frame in self.prices.values():
            dates.update(frame.loc[(frame.index.date >= start) & (frame.index.date <= end)].index)
        return pd.DatetimeIndex(sorted(dates))

    def _has_price(self, ticker: str, timestamp: pd.Timestamp, field: str) -> bool:
        return ticker in self.prices and timestamp in self.prices[ticker].index and pd.notna(self.prices[ticker].at[timestamp, field])

    def _price_on(self, ticker: str, timestamp: pd.Timestamp, field: str) -> float:
        frame = self.prices[ticker]
        if timestamp in frame.index and pd.notna(frame.at[timestamp, field]):
            return float(frame.at[timestamp, field])
        earlier = frame.loc[frame.index <= timestamp, field].dropna()
        return float(earlier.iloc[-1]) if not earlier.empty else 0.0


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result.index = pd.to_datetime(result.index).tz_localize(None)
    return result.sort_index()


def _rebalance_dates(calendar: pd.DatetimeIndex, frequency: str) -> list[date]:
    if frequency == "monthly":
        return [group.iloc[-1].date() for _, group in pd.Series(calendar, index=calendar).groupby(calendar.to_period("M"))]
    if frequency == "quarterly":
        return [group.iloc[-1].date() for _, group in pd.Series(calendar, index=calendar).groupby(calendar.to_period("Q"))]
    raise ValueError(f"Unsupported rebalance frequency: {frequency}")


def _candidate(score: HistoricalScore) -> Candidate:
    return Candidate(score.ticker, score.score, score.coverage, score.sector,
                     score.raw_factors.get("volatility"),
                     has_price=score.exclusion_reason != "missing price",
                     stale_price=score.exclusion_reason == "stale price",
                     sufficient_history=score.exclusion_reason != "insufficient history",
                     liquid=score.exclusion_reason != "liquidity rule")


def _decision(signal: date, execution: date, targets: dict[str, float], construction_excluded: dict[str, str],
              scores: list[HistoricalScore]) -> RebalanceDecision:
    excluded = {score.ticker: score.exclusion_reason for score in scores if score.exclusion_reason}
    excluded.update(construction_excluded)
    return RebalanceDecision(signal, execution, tuple(score.ticker for score in scores), excluded,
        {score.ticker: score.score for score in scores}, {score.ticker: score.coverage for score in scores},
        tuple(targets), targets)


def _trade_statistics(trades: list[SimulatedTradeRecord]) -> dict[str, float | None]:
    opened: dict[str, tuple[date, float]] = {}
    holding_days: list[int] = []
    profitable: list[bool] = []
    for trade in trades:
        if trade.side == "BUY" and trade.ticker not in opened:
            opened[trade.ticker] = (trade.execution_date, trade.execution_price)
        elif trade.side == "SELL" and trade.ticker in opened and trade.post_trade_weight < 1e-9:
            open_date, open_price = opened.pop(trade.ticker)
            holding_days.append((trade.execution_date - open_date).days)
            profitable.append(trade.execution_price > open_price)
    return {"average_holding_period": (sum(holding_days) / len(holding_days) if holding_days else None),
            "profitable_completed_positions": (sum(profitable) / len(profitable) if profitable else None)}
