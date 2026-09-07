#!/usr/bin/env python
"""Explicitly refresh the Donatien External Calibration snapshot.

Separate from every fundamental/research refresh script: this touches only
`current_external_calibration`/`external_calibration_snapshots`. Nothing
here reads or writes StockResearch, LiveResearchRecord, composite_score, or
any existing rating/ranking table. Donatien is EXTERNAL_CALIBRATION per the
project brief -- informational, not a scoring input.

A failed refresh (network failure, missing cal-json container, malformed
JSON, or schema validation failure) never erases the previously valid
current calibration or any historical snapshot -- see
alpha_lab.calibration.service.ExternalCalibrationService.refresh.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_lab.calibration import ExternalCalibrationService  # noqa: E402
from alpha_lab.config import load_settings  # noqa: E402
from alpha_lab.database import create_schema, make_engine  # noqa: E402
from alpha_lab.providers.donatien import DonatienProvider  # noqa: E402
from alpha_lab.providers.errors import ProviderError  # noqa: E402
from alpha_lab.utils.logging import configure_logging  # noqa: E402


def main() -> int:
    configure_logging()
    settings = load_settings()
    engine = make_engine(settings.database_url)
    create_schema(engine)

    service = ExternalCalibrationService(engine)
    provider = DonatienProvider(settings.donatien_url)

    try:
        current = service.refresh(provider)
    except ProviderError as error:
        print(f"Donatien calibration NOT refreshed: {error.kind.value} - {error.reason}")
        print("Previous valid calibration (if any) is unchanged.")
        return 1

    print("Donatien calibration refreshed.")
    print(f"  source: {current.source}")
    print(f"  content_hash: {current.content_hash}")
    print(f"  source_run_date: {current.source_run_date}  source_run_time: {current.source_run_time_raw}")
    print(f"  retrieved_at: {current.retrieved_at}")
    print(f"  supersedes: {current.supersedes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
