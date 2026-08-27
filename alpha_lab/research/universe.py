"""Explicit research-universe membership and bias disclosures."""

from dataclasses import dataclass
from pathlib import Path
import pandas as pd


@dataclass(frozen=True)
class ResearchUniverse:
    tickers: tuple[str, ...]
    name: str
    historical_membership: bool = False

    @property
    def limitation(self) -> str:
        if self.historical_membership:
            return "Historical membership dates supplied."
        return ("SURVIVORSHIP BIAS RISK: this symbol list is not historical constituent data; "
                "results may include securities selected with present-day knowledge.")


def load_universe(path: str | Path) -> ResearchUniverse:
    frame = pd.read_csv(path)
    if "ticker" not in frame:
        raise ValueError("Universe CSV must contain a ticker column")
    tickers = tuple(dict.fromkeys(frame["ticker"].dropna().astype(str).str.upper().str.strip()))
    if not tickers:
        raise ValueError("Universe must contain at least one ticker")
    historical = ("membership_start" in frame and "membership_end" in frame
                  and bool(frame["membership_start"].notna().all()))
    return ResearchUniverse(tickers=tickers, name=Path(path).stem, historical_membership=historical)
