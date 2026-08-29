"""Offline CSV universe provider supporting hundreds or thousands of rows."""

from pathlib import Path
from typing import Any
import pandas as pd

from alpha_lab.providers.interfaces import SecurityUniverseProvider


class CSVSecurityUniverseProvider(SecurityUniverseProvider):
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def get_securities(
        self, *, country: str = "US", exchanges: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        frame = pd.read_csv(self.path)
        if "ticker" not in frame:
            raise ValueError("Universe CSV must include ticker")
        if "country" in frame:
            frame = frame[frame["country"].fillna(country) == country]
        if exchanges and "exchange" in frame:
            frame = frame[frame["exchange"].isin(exchanges)]
        # Object dtype is required: float columns otherwise coerce ``None`` back to NaN.
        clean = frame.astype(object).where(pd.notna(frame), None)
        return clean.to_dict("records")

    def refresh_metadata(self, tickers: list[str]) -> list[dict[str, Any]]:
        wanted = {ticker.upper() for ticker in tickers}
        return [
            row for row in self.get_securities() if str(row["ticker"]).upper() in wanted
        ]
