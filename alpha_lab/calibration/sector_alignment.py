"""CalibrationAlignment (security-level): connects Donatien's sector/tier
weights to individual AlphaLab securities via the approximate GICS bridge
in `alpha_lab.calibration.sector_taxonomy`.

Scope (approved before implementation -- see ARCHITECTURE.md): a pure,
read-time, audit-only DATA CONNECTION, not a new categorical judgment. Given
a security's own `Security.sector` (Morningstar taxonomy, via yfinance) and
the currently stored Donatien calibration, this reports which tiers' weight
lines reference the security's *approximate* GICS sector, and at exactly
the weight Donatien itself published -- never a new computed score, and
never an ALIGNED/CONFLICT/NEUTRAL verdict. That vocabulary belongs to
the Alignment package's cross-evidence-layer comparison (Market Regime vs.
Donatien) and is deliberately not extended to individual securities here --
a security-level directional judgment would require validated,
company-level GICS data this module does not have (see
`sector_taxonomy`'s docstring).

No persistence: computed fresh on every call from already-stored
`CurrentExternalCalibration` + `Security.sector` -- no network call, no new
database table, no snapshot history. Missing/unmapped sector data always
resolves to an empty result, never a fabricated weight.
"""

from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alpha_lab.calibration.sector_taxonomy import approximate_gics_sector
from alpha_lab.calibration.service import DEFAULT_CALIBRATION_SOURCE, ExternalCalibrationService
from alpha_lab.database.models import Security
from alpha_lab.providers.donatien import DonatienCalibration


class SectorTierWeight(BaseModel):
    """One Donatien tier weight line whose `gics_sector` matches a
    security's approximate GICS sector. `pct` is exactly Donatien's own
    published weight for that line -- not recomputed, not normalized."""

    tier: str
    gics_sector: str
    pct: float
    vehicle: str
    line_name: str


def sector_tier_weights(
    calibration: DonatienCalibration, *, morningstar_sector: str | None
) -> list[SectorTierWeight]:
    """Pure lookup: which of `calibration`'s tier weight lines reference
    `morningstar_sector`'s approximate GICS sector, and at what published
    weight. Empty list if the sector is unmapped/unknown or no tier
    references it at all -- never fabricated."""
    gics_sector = approximate_gics_sector(morningstar_sector)
    if gics_sector is None:
        return []
    results: list[SectorTierWeight] = []
    for tier_name, tier in calibration.tiers.items():
        for line_name, line in tier.weights.items():
            if line.gics_sector == gics_sector:
                results.append(SectorTierWeight(
                    tier=tier_name, gics_sector=gics_sector, pct=line.pct,
                    vehicle=line.vehicle, line_name=line_name,
                ))
    return results


def get_sector_tier_weights_for_ticker(
    engine: Engine, ticker: str, *, source: str = DEFAULT_CALIBRATION_SOURCE
) -> list[SectorTierWeight]:
    """Convenience read: resolves `ticker`'s stored `Security.sector` and
    the currently stored Donatien calibration, then applies
    `sector_tier_weights`. Pure database reads only -- no network call.
    Returns an empty list (never a fabricated weight) if the security is
    unknown, has no stored sector, or no calibration has been refreshed
    yet."""
    with Session(engine) as session:
        security = session.get(Security, ticker)
        morningstar_sector = None if security is None else security.sector

    calibration = ExternalCalibrationService(engine).get_current_calibration(source)
    if calibration is None:
        return []
    return sector_tier_weights(calibration, morningstar_sector=morningstar_sector)
