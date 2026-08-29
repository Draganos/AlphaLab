"""Credential-free NASDAQ Trader universe discovery with explicit provenance."""

from io import StringIO
from typing import Any
from urllib.request import Request, urlopen

import pandas as pd

from alpha_lab.providers.interfaces import SecurityUniverseProvider


class NasdaqTraderUniverseProvider(SecurityUniverseProvider):
    """Load active non-ETF NYSE/NASDAQ symbols; metadata enrichment is separate."""

    urls = {
        "NASDAQ": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "OTHER": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    }

    def get_securities(
        self, *, country: str = "US", exchanges: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        rows = self._nasdaq_rows() + self._other_rows()
        allowed = {item.upper() for item in exchanges}
        if allowed:
            rows = [row for row in rows if row["exchange"] in allowed]
        return rows

    def refresh_metadata(self, tickers: list[str]) -> list[dict[str, Any]]:
        wanted = {ticker.upper() for ticker in tickers}
        return [row for row in self.get_securities() if row["ticker"] in wanted]

    def _download(self, market: str) -> pd.DataFrame:
        request = Request(
            self.urls[market],
            headers={"User-Agent": "AlphaLab research contact@example.invalid"},
        )
        with urlopen(request, timeout=45) as response:  # noqa: S310 - fixed provider endpoints
            content = response.read().decode("utf-8")
        return pd.read_csv(StringIO(content), sep="|")

    def _nasdaq_rows(self) -> list[dict[str, Any]]:
        frame = self._download("NASDAQ")
        frame = frame[(frame["ETF"] == "N") & (frame["Test Issue"] == "N")]
        return [
            self._row(row["Symbol"], row["Security Name"], "NASDAQ")
            for _, row in frame.iterrows()
            if _likely_common_stock(str(row["Security Name"]))
        ]

    def _other_rows(self) -> list[dict[str, Any]]:
        frame = self._download("OTHER")
        frame = frame[(frame["ETF"] == "N") & (frame["Test Issue"] == "N")]
        exchange_names = {
            "N": "NYSE",
            "A": "NYSE American",
            "P": "NYSE Arca",
            "Z": "Cboe",
        }
        return [
            self._row(
                row["ACT Symbol"],
                row["Security Name"],
                exchange_names.get(row["Exchange"], row["Exchange"]),
            )
            for _, row in frame.iterrows()
            if row["Exchange"] in {"N", "A"}
            and _likely_common_stock(str(row["Security Name"]))
        ]

    @staticmethod
    def _row(ticker: str, name: str, exchange: str) -> dict[str, Any]:
        return {
            "ticker": str(ticker).upper(),
            "company_name": str(name),
            "exchange": exchange,
            "country": "US",
            "sector": None,
            "industry": None,
            "currency": "USD",
            "asset_type": "equity",
            "market_cap": None,
            "business_description": None,
            "metadata_provider": "NasdaqTraderUniverseProvider",
            "metadata_source": "NASDAQ Trader Symbol Directory",
        }


def _likely_common_stock(name: str) -> bool:
    lowered = name.casefold()
    excluded = (
        "warrant",
        "rights",
        " units",
        "preferred",
        "depositary",
        "notes due",
        "bond",
        "fund",
    )
    return not any(term in lowered for term in excluded)
