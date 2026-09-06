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


def _number(value: Any, *, positive: bool = False) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number
