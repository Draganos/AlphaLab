"""Explicit freshness, completeness, and capability reporting."""

from datetime import date
from enum import StrEnum
import math
from pydantic import BaseModel


class QualityStatus(StrEnum):
    OK = "ok"
    MISSING = "missing"
    STALE = "stale"
    UNSUPPORTED = "unsupported"


class DataQualityIssue(BaseModel):
    field: str
    status: QualityStatus
    detail: str


def assess_field(field: str, value: object, *, supported: bool = True) -> DataQualityIssue | None:
    if not supported:
        return DataQualityIssue(field=field, status=QualityStatus.UNSUPPORTED, detail="Provider does not support this field")
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return DataQualityIssue(field=field, status=QualityStatus.MISSING, detail="No observation is available")
    return None


def assess_freshness(field: str, observed: date | None, evaluation_date: date, stale_after_days: int) -> DataQualityIssue | None:
    if observed is None:
        return DataQualityIssue(field=field, status=QualityStatus.MISSING, detail="No observation date is available")
    age = (evaluation_date - observed).days
    if age > stale_after_days:
        return DataQualityIssue(field=field, status=QualityStatus.STALE,
                                detail=f"Observation is {age} days old; limit is {stale_after_days}")
    return None
