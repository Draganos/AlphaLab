from datetime import date
from alpha_lab.data_quality import QualityStatus, assess_field, assess_freshness


def test_missing_unsupported_and_stale_are_distinct():
    assert assess_field("eps", None).status is QualityStatus.MISSING
    assert assess_field("estimates", None, supported=False).status is QualityStatus.UNSUPPORTED
    assert assess_freshness("price", date(2024, 1, 1), date(2024, 1, 10), 5).status is QualityStatus.STALE
    assert assess_freshness("price", date(2024, 1, 9), date(2024, 1, 10), 5) is None
