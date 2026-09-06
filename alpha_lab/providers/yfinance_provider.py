"""Optional US data provider. Missing fields remain null.

External call failures (rate limiting, network/DNS unavailability, an
unclassified provider error) are never treated as data -- they raise
``ProviderError`` for the caller to handle, instead of surfacing raw
yfinance/curl_cffi/requests internals or being silently swallowed into a
zero, an empty frame, or a fabricated value.
"""

from datetime import date
from typing import Any
import math
import pandas as pd

from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import call_with_classification


class YFinanceProvider(MarketDataProvider):
    def _ticker(self, symbol: str):
        import yfinance as yf

        return yf.Ticker(symbol)

    def get_price_history(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        frame = call_with_classification(
            lambda: self._ticker(ticker).history(start=start, end=end, auto_adjust=False),
            provider=self.provider_name,
        )
        if frame.empty:
            return frame
        frame = frame.rename(columns={"Adj Close": "adjusted_close"})
        frame.columns = [
            str(column).lower().replace(" ", "_") for column in frame.columns
        ]
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
        return frame[
            [
                c
                for c in ["open", "high", "low", "close", "adjusted_close", "volume"]
                if c in frame
            ]
        ]

    def get_company_info(self, ticker: str) -> dict[str, Any]:
        info = call_with_classification(
            lambda: self._ticker(ticker).get_info(),
            provider=self.provider_name,
        )
        return {
            "ticker": ticker.upper(),
            "company_name": info.get("longName"),
            "exchange": info.get("exchange"),
            "country": info.get("country"),
            "sector": info.get("sector"),
            "currency": info.get("currency"),
            "industry": info.get("industry"),
            "asset_type": info.get("quoteType"),
            "market_cap": _number(info.get("marketCap"), positive=True),
            "business_description": info.get("longBusinessSummary"),
            "metadata_provider": self.provider_name,
            "metadata_source": "yfinance quoteSummary",
        }

    def get_financials(self, ticker: str) -> pd.DataFrame:
        ticker_obj = self._ticker(ticker)
        statement = call_with_classification(
            lambda: ticker_obj.quarterly_income_stmt,
            provider=self.provider_name,
        )
        cashflow = call_with_classification(
            lambda: ticker_obj.quarterly_cashflow,
            provider=self.provider_name,
        )
        balance = call_with_classification(
            lambda: ticker_obj.quarterly_balance_sheet,
            provider=self.provider_name,
        )
        if statement.empty:
            return pd.DataFrame()
        rows: list[dict[str, Any]] = []
        for period in statement.columns:

            def value(frame: pd.DataFrame, label: str) -> float | None:
                if (
                    label not in frame.index
                    or period not in frame.columns
                    or pd.isna(frame.at[label, period])
                ):
                    return None
                return _number(frame.at[label, period])

            rows.append(
                {
                    "period": pd.Timestamp(period).date(),
                    "publication_date": None,
                    "revenue": value(statement, "Total Revenue"),
                    "ebitda": value(statement, "EBITDA"),
                    "ebit": value(statement, "EBIT"),
                    "net_income": value(statement, "Net Income"),
                    "eps": value(statement, "Diluted EPS"),
                    "free_cash_flow": value(cashflow, "Free Cash Flow"),
                    "total_debt": value(balance, "Total Debt"),
                    "cash": value(
                        balance, "Cash Cash Equivalents And Short Term Investments"
                    ),
                    "total_equity": value(balance, "Stockholders Equity"),
                    "shares_outstanding": value(balance, "Ordinary Shares Number"),
                    "gross_profit": value(statement, "Gross Profit"),
                    "total_assets": value(balance, "Total Assets"),
                    "current_assets": value(balance, "Current Assets"),
                    "current_liabilities": value(balance, "Current Liabilities"),
                    "interest_expense": value(statement, "Interest Expense"),
                    "dividends_paid": value(cashflow, "Cash Dividends Paid"),
                    "share_repurchases": value(cashflow, "Repurchase Of Capital Stock"),
                }
            )
        return pd.DataFrame(rows)

    def get_analyst_consensus(self, ticker: str) -> dict[str, Any]:
        """Raw analyst recommendation counts + price targets for `ticker`.

        Returns a plain dict of raw inputs (not the canonical
        ``AnalystConsensus`` model) -- consistent with ``get_company_info``'s
        existing style. ``alpha_lab.research.analyst_consensus.build_analyst_consensus``
        turns this into the canonical, rated object; that mapping is kept
        pure and provider-independent so it can be unit-tested without a
        provider at all.

        Only the current ("0m") recommendationTrend row is used -- the
        other rows the endpoint returns are trailing months of the same
        trend, which is Analyst *Revisions* territory (out of scope here;
        see the module docstring in analyst_consensus.py).

        `as_of` is today's date (when AlphaLab made this call) -- Yahoo's
        recommendationTrend/financialData modules do not expose a
        provider-side observation timestamp, so this is never presented as
        one.
        """
        ticker_obj = self._ticker(ticker)
        recommendations = call_with_classification(
            lambda: ticker_obj.get_recommendations(),
            provider=self.provider_name,
        )
        targets = call_with_classification(
            lambda: ticker_obj.get_analyst_price_targets(),
            provider=self.provider_name,
        )
        counts = _current_recommendation_counts(recommendations)
        return {
            "ticker": ticker.upper(),
            "as_of": date.today(),
            "strong_buy": counts.get("strongBuy"),
            "buy": counts.get("buy"),
            "hold": counts.get("hold"),
            "sell": counts.get("sell"),
            "strong_sell": counts.get("strongSell"),
            "target_current": _number(targets.get("current"), positive=True),
            "target_low": _number(targets.get("low"), positive=True),
            "target_mean": _number(targets.get("mean"), positive=True),
            "target_median": _number(targets.get("median"), positive=True),
            "target_high": _number(targets.get("high"), positive=True),
            "source": self.provider_name,
        }


def _current_recommendation_counts(frame: pd.DataFrame) -> dict[str, int | None]:
    """The "0m" (current month) row of yfinance's recommendationTrend, or an
    empty dict if that row is missing -- never a guessed/zeroed fallback."""
    if frame is None or frame.empty or "period" not in frame:
        return {}
    current = frame[frame["period"] == "0m"]
    if current.empty:
        return {}
    row = current.iloc[0]
    result: dict[str, int | None] = {}
    for column in ("strongBuy", "buy", "hold", "sell", "strongSell"):
        if column not in row or pd.isna(row[column]):
            result[column] = None
        else:
            result[column] = int(row[column])
    return result


def _number(value: Any, *, positive: bool = False) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number
