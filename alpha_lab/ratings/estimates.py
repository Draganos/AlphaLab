"""Point-in-time analyst revision factors from genuine timestamped snapshots."""

from datetime import date, timedelta
import numpy as np
import pandas as pd


def calculate_revision_factors(
    observations: pd.DataFrame, as_of: date
) -> dict[str, float | int | None]:
    if observations.empty or "observation_date" not in observations:
        return _empty()
    frame = observations.copy()
    frame["observation_date"] = pd.to_datetime(frame["observation_date"])
    frame = frame.loc[frame["observation_date"] <= pd.Timestamp(as_of)].sort_values(
        "observation_date"
    )
    if frame.empty:
        return _empty()
    if "provider" in frame:
        providers = frame.loc[frame["provider"].notna(), "provider"].unique()
        if len(providers):
            provider = max(
                providers,
                key=lambda name: (
                    frame.loc[frame["provider"] == name, "observation_date"].max(),
                    str(name),
                ),
            )
            frame = frame.loc[frame["provider"] == provider]
    current = frame.iloc[-1]
    result: dict[str, float | int | None] = {
        "current_consensus_eps": _number(current.get("consensus_eps")),
        "current_consensus_revenue": _number(current.get("consensus_revenue")),
        "analyst_count": int(current["analyst_count"])
        if pd.notna(current.get("analyst_count"))
        else None,
        "estimate_dispersion": _number(current.get("estimate_dispersion")),
    }
    for days in (7, 30, 90):
        result[f"eps_revision_{days}d"] = _revision(
            frame, current, "consensus_eps", as_of - timedelta(days=days)
        )
        result[f"revenue_revision_{days}d"] = _revision(
            frame, current, "consensus_revenue", as_of - timedelta(days=days)
        )
    eps = (
        frame["consensus_eps"].dropna()
        if "consensus_eps" in frame
        else pd.Series(dtype=float)
    )
    changes = eps.diff().dropna()
    result["upward_revisions"] = int((changes > 0).sum()) if len(eps) >= 2 else None
    result["downward_revisions"] = int((changes < 0).sum()) if len(eps) >= 2 else None
    return result


def _revision(
    frame: pd.DataFrame, current: pd.Series, column: str, cutoff: date
) -> float | None:
    if column not in frame or pd.isna(current.get(column)):
        return None
    current_date = pd.Timestamp(current["observation_date"])
    prior = frame.loc[
        (frame["observation_date"] <= pd.Timestamp(cutoff))
        & (frame["observation_date"] < current_date),
        column,
    ].dropna()
    if prior.empty or prior.iloc[-1] == 0:
        return None
    return float(
        current[column] / abs(prior.iloc[-1]) - (1 if prior.iloc[-1] > 0 else -1)
    )


def _number(value) -> float | None:
    return (
        None
        if value is None or pd.isna(value) or not np.isfinite(value)
        else float(value)
    )


def _empty() -> dict[str, None]:
    return {
        name: None
        for name in [
            "current_consensus_eps",
            "current_consensus_revenue",
            "analyst_count",
            "estimate_dispersion",
            "eps_revision_7d",
            "eps_revision_30d",
            "eps_revision_90d",
            "revenue_revision_7d",
            "revenue_revision_30d",
            "revenue_revision_90d",
            "upward_revisions",
            "downward_revisions",
        ]
    }
