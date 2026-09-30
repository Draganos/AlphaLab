from datetime import date
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Fundamental, Price, Security
from alpha_lab.database.queries import latest_fundamentals_as_of
from alpha_lab.ingestion import IngestionService
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import ProviderError, ProviderErrorKind
import pytest


class FakeProvider(MarketDataProvider):
    def get_company_info(self, ticker): return {"ticker": ticker, "company_name": "Fixture", "country": "US", "currency": "USD"}
    def get_price_history(self, ticker, start, end):
        return pd.DataFrame({"close": [10.0], "adjusted_close": [10.0]}, index=pd.to_datetime(["2024-01-01"]))
    def get_financials(self, ticker):
        return pd.DataFrame([{"period": date(2023, 12, 31), "publication_date": None, "eps": 1.0}])


def test_ingestion_is_idempotent_and_preserves_unknown_publication_date():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    service = IngestionService(FakeProvider(), engine)
    service.ingest("abc", date(2024, 1, 1), date(2024, 2, 1))
    service.ingest("abc", date(2024, 1, 1), date(2024, 2, 1))
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Price)) == 1
        fundamental = session.scalar(select(Fundamental))
        assert fundamental.publication_date is None
        assert fundamental.provider == "FakeProvider"
        assert fundamental.currency == "USD"


class RevisingProvider(FakeProvider):
    def get_financials(self, ticker):
        return pd.DataFrame([
            {"period": date(2023, 12, 31), "publication_date": date(2024, 2, 1),
             "eps": 1.0, "source": "original filing"},
            {"period": date(2023, 12, 31), "publication_date": date(2024, 4, 1),
             "eps": 1.2, "source": "restated filing"},
        ])


def test_fundamental_revisions_are_append_only_idempotent_and_point_in_time():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    service = IngestionService(RevisingProvider(), engine)
    service.ingest("REV", date(2024, 1, 1), date(2024, 5, 1))
    service.ingest("REV", date(2024, 1, 1), date(2024, 5, 1))
    with Session(engine) as session:
        versions = session.scalars(select(Fundamental).order_by(Fundamental.publication_date)).all()
        assert len(versions) == 2
        assert [version.eps for version in versions] == [1.0, 1.2]
        before = latest_fundamentals_as_of(session, "REV", date(2024, 3, 1))
        after = latest_fundamentals_as_of(session, "REV", date(2024, 5, 1))
        assert len(before) == len(after) == 1
        assert before[0].eps == 1.0
        assert before[0].source == "original filing"
        assert after[0].eps == 1.2
        assert after[0].source == "restated filing"


class RevisingPriceProvider(FakeProvider):
    def __init__(self, close: float):
        self._close = close

    def get_price_history(self, ticker, start, end):
        return pd.DataFrame(
            {"close": [self._close], "adjusted_close": [self._close]},
            index=pd.to_datetime(["2024-01-01"]),
        )


def test_price_revisions_are_append_only_not_mutated_in_place():
    """Real reviewer-caught PIT bug (see FXRateService.refresh's identical
    fix): a genuine price revision must append a new row with a fresh
    ingested_at, never mutate the existing row in place -- mutating would
    silently let a historical as_of between the two ingests see the
    revision, data AlphaLab did not actually possess at that point."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    IngestionService(RevisingPriceProvider(10.0), engine).ingest(
        "REV", date(2024, 1, 1), date(2024, 2, 1)
    )
    IngestionService(RevisingPriceProvider(11.0), engine).ingest(
        "REV", date(2024, 1, 1), date(2024, 2, 1)
    )
    with Session(engine) as session:
        rows = session.scalars(
            select(Price).where(Price.ticker == "REV").order_by(Price.id)
        ).all()
        assert len(rows) == 2  # appended, never mutated
        assert [row.close for row in rows] == [10.0, 11.0]


def test_price_re_ingesting_an_unchanged_bar_is_a_true_no_op():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    service = IngestionService(RevisingPriceProvider(10.0), engine)
    service.ingest("SAME", date(2024, 1, 1), date(2024, 2, 1))
    service.ingest("SAME", date(2024, 1, 1), date(2024, 2, 1))
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(Price).where(Price.ticker == "SAME")
        ) == 1


class _AdjustedCloseProvider(FakeProvider):
    def __init__(self, close: float, adjusted_close: float):
        self._close, self._adjusted = close, adjusted_close

    def get_price_history(self, ticker, start, end):
        return pd.DataFrame(
            {"close": [self._close], "adjusted_close": [self._adjusted]},
            index=pd.to_datetime(["2024-01-01"]),
        )


def _price_row_count(engine, ticker):
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Price).where(Price.ticker == ticker))


def test_float_precision_jitter_in_adjusted_close_is_not_a_revision():
    """Regression: re-fetching an unchanged bar returns adjusted_close
    differing in the last float32 bits (~1e-7 relative, measured on the live
    database). Exact `!=` appended a duplicate row for it on every refresh
    (14% surplus rows after three ingests)."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    IngestionService(_AdjustedCloseProvider(399.6000061035156, 397.08087158203125), engine).ingest(
        "JIT", date(2024, 1, 1), date(2024, 2, 1)
    )
    IngestionService(_AdjustedCloseProvider(399.6000061035156, 397.0808410644531), engine).ingest(
        "JIT", date(2024, 1, 1), date(2024, 2, 1)
    )
    assert _price_row_count(engine, "JIT") == 1


def test_a_genuine_dividend_restatement_of_adjusted_close_still_appends():
    """The tolerance must not swallow real revisions: a 0.1% restatement
    (the smallest genuine dividend adjustment observed) is still a change."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    IngestionService(_AdjustedCloseProvider(100.0, 100.0), engine).ingest("DIV", date(2024, 1, 1), date(2024, 2, 1))
    IngestionService(_AdjustedCloseProvider(100.0, 99.9), engine).ingest("DIV", date(2024, 1, 1), date(2024, 2, 1))
    assert _price_row_count(engine, "DIV") == 2


def test_a_change_from_or_to_a_missing_value_is_still_a_revision():
    from alpha_lab.ingestion.service import _price_field_changed

    assert _price_field_changed(None, 1.0)
    assert _price_field_changed(1.0, None)
    assert not _price_field_changed(None, None)
    assert _price_field_changed("a", "b")
    assert not _price_field_changed(1_000_000, 1_000_000)
    assert _price_field_changed(592.0, 165697.0)  # volume restated


class _RawProvider(FakeProvider):
    """Returns exactly the frames it is given -- for hostile-payload tests."""

    def __init__(self, prices=None, financials=None):
        self._prices, self._financials = prices, financials

    def get_price_history(self, ticker, start, end):
        return self._prices if self._prices is not None else super().get_price_history(ticker, start, end)

    def get_financials(self, ticker):
        return self._financials if self._financials is not None else pd.DataFrame()


def _counts(engine, ticker):
    with Session(engine) as session:
        return (
            session.scalar(select(func.count()).select_from(Price).where(Price.ticker == ticker)),
            session.scalar(select(func.count()).select_from(Fundamental).where(Fundamental.ticker == ticker)),
        )


def test_a_price_row_with_no_date_is_skipped_not_fatal(caplog):
    """Regression (found by payload fuzzing): a NaT row date raised mid-ingest,
    aborting the whole ticker and rolling back every good row with it."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    index = pd.DatetimeIndex(["2024-01-02", pd.NaT, "2024-01-04"])
    frame = pd.DataFrame({"close": [1.0, 2.0, 3.0], "adjusted_close": [1.0, 2.0, 3.0]}, index=index)
    with caplog.at_level("WARNING", logger="alpha_lab.ingestion.service"):
        IngestionService(_RawProvider(prices=frame), engine).ingest("NAT", date(2024, 1, 1), date(2024, 2, 1))
    assert _counts(engine, "NAT")[0] == 2
    assert any(record.message == "ingestion_rows_skipped" for record in caplog.records)


def test_a_financial_row_with_an_unusable_period_is_skipped_not_fatal():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    financials = pd.DataFrame([
        {"period": date(2023, 12, 31), "publication_date": None, "eps": 1.0},
        {"period": "not a date", "publication_date": None, "eps": 2.0},
        {"period": pd.NaT, "publication_date": None, "eps": 3.0},
    ])
    IngestionService(_RawProvider(financials=financials), engine).ingest("BADP", date(2024, 1, 1), date(2024, 2, 1))
    assert _counts(engine, "BADP") == (1, 1)  # prices and the one good period both stored


def test_non_numeric_strings_and_nonfinite_numbers_become_none_not_errors():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    index = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    frame = pd.DataFrame(
        {"close": ["1.5", "abc", float("inf")], "adjusted_close": [1.0, float("nan"), 3.0]}, index=index
    )
    IngestionService(_RawProvider(prices=frame), engine).ingest("STR", date(2024, 1, 1), date(2024, 2, 1))
    with Session(engine) as session:
        closes = [row.close for row in session.scalars(select(Price).where(Price.ticker == "STR").order_by(Price.date))]
    assert closes == [1.5, None, None]


def test_unparseable_publication_date_is_none_not_an_error():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    financials = pd.DataFrame([{"period": date(2023, 12, 31), "publication_date": "garbage", "eps": 1.0}])
    IngestionService(_RawProvider(financials=financials), engine).ingest("PUB", date(2024, 1, 1), date(2024, 2, 1))
    with Session(engine) as session:
        assert session.scalar(select(Fundamental.publication_date).where(Fundamental.ticker == "PUB")) is None


def test_market_provider_cannot_replace_canonical_universe_exchange():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker="ABC", exchange="NASDAQ"))
        session.commit()

    class ExchangeProvider(FakeProvider):
        def get_company_info(self, ticker):
            return {**super().get_company_info(ticker), "exchange": "NYQ"}

    IngestionService(ExchangeProvider(), engine).ingest(
        "ABC", date(2024, 1, 1), date(2024, 2, 1)
    )
    with Session(engine) as session:
        assert session.get(Security, "ABC").exchange == "NASDAQ"


def test_provider_failure_or_missing_refresh_does_not_erase_valid_price():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    service = IngestionService(FakeProvider(), engine)
    service.ingest("SAFE", date(2024, 1, 1), date(2024, 2, 1))

    class MissingProvider(FakeProvider):
        def get_price_history(self, ticker, start, end):
            return pd.DataFrame(
                {"close": [float("nan")], "adjusted_close": [float("inf")]},
                index=pd.to_datetime(["2024-01-01"]),
            )

    IngestionService(MissingProvider(), engine).ingest(
        "SAFE", date(2024, 1, 1), date(2024, 2, 1)
    )
    with Session(engine) as session:
        price = session.scalar(select(Price).where(Price.ticker == "SAFE"))
        assert price.close == price.adjusted_close == 10


class FailingProvider(FakeProvider):
    def get_company_info(self, ticker):
        raise ProviderError(ProviderErrorKind.RATE_LIMITED, "FailingProvider", "Yahoo Finance rate-limited the request")


def test_provider_error_propagates_before_any_database_write_and_leaves_prior_data_intact():
    """A rate-limited/network-unavailable provider failure must surface as a
    ProviderError to the caller (never be swallowed) and must never touch
    the database for that ticker -- existing valid rows from a prior
    successful ingest must survive untouched."""
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    IngestionService(FakeProvider(), engine).ingest("NVDA", date(2024, 1, 1), date(2024, 2, 1))

    with pytest.raises(ProviderError) as excinfo:
        IngestionService(FailingProvider(), engine).ingest("NVDA", date(2024, 1, 1), date(2024, 2, 1))
    assert excinfo.value.kind == ProviderErrorKind.RATE_LIMITED

    with Session(engine) as session:
        price = session.scalar(select(Price).where(Price.ticker == "NVDA"))
        assert price is not None
        assert price.close == 10
