"""Optional US data provider. Missing fields remain null."""

from datetime import date
from typing import Any
import pandas as pd

from alpha_lab.providers.base import MarketDataProvider


class YFinanceProvider(MarketDataProvider):
    def _ticker(self, symbol: str):
        import yfinance as yf
        return yf.Ticker(symbol)

    def get_price_history(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        frame = self._ticker(ticker).history(start=start, end=end, auto_adjust=False)
        if frame.empty:
            return frame
        frame = frame.rename(columns={"Adj Close": "adjusted_close"})
        frame.columns = [str(column).lower().replace(" ", "_") for column in frame.columns]
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
        return frame[[c for c in ["open", "high", "low", "close", "adjusted_close", "volume"] if c in frame]]

    def get_company_info(self, ticker: str) -> dict[str, Any]:
        info = self._ticker(ticker).get_info()
        return {
            "ticker": ticker.upper(), "company_name": info.get("longName"),
            "exchange": info.get("exchange"), "country": info.get("country"),
            "sector": info.get("sector"), "currency": info.get("currency"),
            "asset_type": info.get("quoteType"),
        }

    def get_financials(self, ticker: str) -> pd.DataFrame:
        statement = self._ticker(ticker).quarterly_income_stmt
        cashflow = self._ticker(ticker).quarterly_cashflow
        balance = self._ticker(ticker).quarterly_balance_sheet
        if statement.empty:
            return pd.DataFrame()
        rows: list[dict[str, Any]] = []
        for period in statement.columns:
            def value(frame: pd.DataFrame, label: str) -> float | None:
                if label not in frame.index or period not in frame.columns or pd.isna(frame.at[label, period]):
                    return None
                return float(frame.at[label, period])
            rows.append({
                "period": pd.Timestamp(period).date(), "publication_date": None,
                "revenue": value(statement, "Total Revenue"), "ebitda": value(statement, "EBITDA"),
                "ebit": value(statement, "EBIT"), "net_income": value(statement, "Net Income"),
                "eps": value(statement, "Diluted EPS"), "free_cash_flow": value(cashflow, "Free Cash Flow"),
                "total_debt": value(balance, "Total Debt"), "cash": value(balance, "Cash Cash Equivalents And Short Term Investments"),
                "total_equity": value(balance, "Stockholders Equity"), "shares_outstanding": value(balance, "Ordinary Shares Number"),
            })
        return pd.DataFrame(rows)
