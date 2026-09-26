"""Tests for alpha_lab.fx.FXRateService: point-in-time currency -> USD
conversion and its real-provider ingestion, entirely against an in-memory
database -- no network. See tests/test_yfinance_provider.py for
YFinanceProvider.get_fx_rate_history's own offline tests."""

from datetime import date, datetime, timedelta

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import FXRate
from alpha_lab.database.session import session_scope
from alpha_lab.fx import FXRateService

# Every FXRate row's `ingested_at` defaults to "now" -- happy-path tests use
# dates at or before today so that default is never itself the reason a
# lookup misses (that PIT gate has its own dedicated test below).
_TODAY = date.today()


def _engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    return engine


class _FakeFXProvider:
    provider_name = "FakeFXProvider"

    def __init__(self, rows_by_currency: dict[str, list[dict]]):
        self._rows_by_currency = rows_by_currency

    def get_fx_rate_history(self, currency, start, end):
        return self._rows_by_currency.get(currency, [])


# --- convert_to_usd: pure database reads --------------------------------


def test_convert_to_usd_passes_amount_through_unchanged_for_usd():
    service = FXRateService(_engine())
    assert service.convert_to_usd(1_000.0, "USD", date(2026, 1, 1)) == 1_000.0


def test_convert_to_usd_passes_amount_through_unchanged_for_unknown_currency():
    # Backward-compatible with every LiveResearchRecord predating the
    # currency field -- confirmed live to actually be USD.
    service = FXRateService(_engine())
    assert service.convert_to_usd(1_000.0, None, date(2026, 1, 1)) == 1_000.0


def test_convert_to_usd_returns_none_for_none_amount():
    service = FXRateService(_engine())
    assert service.convert_to_usd(None, "AED", date(2026, 1, 1)) is None


def test_convert_to_usd_returns_none_when_no_rate_has_ever_been_ingested():
    # Never fabricated -- the same "missing != guessed" rule as every
    # other optional field in this codebase.
    service = FXRateService(_engine())
    assert service.convert_to_usd(1_000.0, "AED", _TODAY) is None


def test_convert_to_usd_converts_using_the_real_ingested_rate():
    engine = _engine()
    with session_scope(engine) as session:
        session.add(FXRate(currency="AED", date=_TODAY, rate_to_usd=0.2724))
    service = FXRateService(engine)
    assert service.convert_to_usd(1_000.0, "aed", _TODAY) == 272.4


def test_convert_to_usd_uses_the_most_recent_rate_at_or_before_as_of():
    engine = _engine()
    earlier, later = date(2020, 1, 1), date(2020, 1, 5)
    long_ago = datetime(2020, 1, 1)  # well before any as_of used below
    with session_scope(engine) as session:
        session.add(
            FXRate(currency="AED", date=earlier, rate_to_usd=0.27, ingested_at=long_ago)
        )
        session.add(
            FXRate(currency="AED", date=later, rate_to_usd=0.28, ingested_at=long_ago)
        )
    service = FXRateService(engine)
    assert service.convert_to_usd(1_000.0, "AED", date(2020, 1, 3)) == 270.0
    assert service.convert_to_usd(1_000.0, "AED", _TODAY) == 280.0


def test_convert_to_usd_never_sees_a_rate_from_after_as_of():
    engine = _engine()
    with session_scope(engine) as session:
        session.add(FXRate(currency="AED", date=_TODAY, rate_to_usd=0.28))
    service = FXRateService(engine)
    assert service.convert_to_usd(1_000.0, "AED", _TODAY - timedelta(days=5)) is None


def test_convert_to_usd_is_pit_safe_on_ingested_at_not_just_date():
    """Mirrors get_technical_summary_as_of's own rationale: a rate row
    dated in the past can still have been inserted only today (a first
    refresh_fx_rates.py run ingests real history in one call), so a
    historical as_of read must never see a rate not yet actually stored as
    of that date."""
    engine = _engine()
    with session_scope(engine) as session:
        session.add(
            FXRate(
                currency="AED",
                date=date(2020, 1, 1),
                rate_to_usd=0.27,
                ingested_at=datetime.now() + timedelta(days=1),
            )
        )
    service = FXRateService(engine)
    assert service.convert_to_usd(1_000.0, "AED", date.today()) is None


# --- refresh: ingestion via a fake provider, no network ------------------


def test_refresh_is_a_no_op_for_usd():
    engine = _engine()
    service = FXRateService(engine)
    provider = _FakeFXProvider({"USD": [{"date": date(2026, 1, 1), "rate_to_usd": 1.0}]})
    assert service.refresh(provider, "USD", start=date(2026, 1, 1), end=date(2026, 1, 2)) == 0
    with session_scope(engine) as session:
        assert session.query(FXRate).count() == 0


def test_refresh_ingests_real_provider_rows():
    engine = _engine()
    service = FXRateService(engine)
    yesterday = _TODAY - timedelta(days=1)
    provider = _FakeFXProvider(
        {
            "AED": [
                {"date": yesterday, "rate_to_usd": 0.2724},
                {"date": _TODAY, "rate_to_usd": 0.2723},
            ]
        }
    )
    count = service.refresh(provider, "aed", start=yesterday, end=_TODAY)
    assert count == 2
    assert service.convert_to_usd(1_000.0, "AED", _TODAY) == 272.3


def test_refresh_re_ingesting_an_unchanged_rate_is_a_true_no_op():
    engine = _engine()
    service = FXRateService(engine)
    first = _FakeFXProvider({"AED": [{"date": _TODAY, "rate_to_usd": 0.27}]})
    assert service.refresh(first, "AED", start=_TODAY, end=_TODAY) == 1
    same_again = _FakeFXProvider({"AED": [{"date": _TODAY, "rate_to_usd": 0.27}]})
    assert service.refresh(same_again, "AED", start=_TODAY, end=_TODAY) == 0
    with session_scope(engine) as session:
        assert session.query(FXRate).count() == 1
    assert service.convert_to_usd(1_000.0, "AED", _TODAY) == 270.0


def test_refresh_appends_a_revision_rather_than_mutating_the_existing_row():
    """A real reviewer finding: refresh() must never mutate an existing
    row's rate_to_usd in place, or a later revision silently overwrites
    what a historical as_of between the two refreshes would see."""
    engine = _engine()
    service = FXRateService(engine)
    day = date(2026, 1, 5)
    original = _FakeFXProvider({"AED": [{"date": day, "rate_to_usd": 0.2720}]})
    service.refresh(original, "AED", start=day, end=day)
    revised = _FakeFXProvider({"AED": [{"date": day, "rate_to_usd": 0.2730}]})
    count = service.refresh(revised, "AED", start=day, end=day)
    assert count == 1  # the revision is a new observation, not a skip
    with session_scope(engine) as session:
        assert session.query(FXRate).count() == 2  # appended, never mutated


def test_convert_to_usd_never_leaks_a_later_revision_into_an_earlier_as_of():
    """The exact PIT-leak scenario a real review of this PR found: without
    append-only revisions, a later-discovered restated rate would become
    visible to a historical as_of that predates the restatement -- data
    AlphaLab did not actually possess at that point in its own history."""
    engine = _engine()
    service = FXRateService(engine)
    day = date(2026, 1, 5)
    first_refresh_at = datetime(2026, 9, 26)
    second_refresh_at = datetime(2026, 9, 30)

    original = _FakeFXProvider({"AED": [{"date": day, "rate_to_usd": 0.2720}]})
    service.refresh(original, "AED", start=day, end=day)
    with session_scope(engine) as session:
        row = session.query(FXRate).one()
        row.ingested_at = first_refresh_at

    revised = _FakeFXProvider({"AED": [{"date": day, "rate_to_usd": 0.2730}]})
    service.refresh(revised, "AED", start=day, end=day)
    with session_scope(engine) as session:
        rows = session.query(FXRate).order_by(FXRate.id).all()
        assert len(rows) == 2
        rows[1].ingested_at = second_refresh_at

    between_the_two_refreshes = date(2026, 9, 28)
    assert service.convert_to_usd(1_000.0, "AED", between_the_two_refreshes) == 272.0
    assert service.convert_to_usd(1_000.0, "AED", date(2026, 9, 30)) == 273.0


def test_refresh_skips_non_finite_or_non_positive_rates_never_fabricating_a_conversion():
    engine = _engine()
    service = FXRateService(engine)
    provider = _FakeFXProvider(
        {
            "AED": [
                {"date": date(2026, 1, 1), "rate_to_usd": float("nan")},
                {"date": date(2026, 1, 2), "rate_to_usd": -1.0},
                {"date": date(2026, 1, 3), "rate_to_usd": 0.0},
            ]
        }
    )
    count = service.refresh(provider, "AED", start=date(2026, 1, 1), end=date(2026, 1, 4))
    assert count == 0
    with session_scope(engine) as session:
        assert session.query(FXRate).count() == 0
