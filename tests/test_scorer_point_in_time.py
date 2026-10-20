"""The historical scorer must score a past date from the price versions
AlphaLab held on that date: a later revision of a bar (a restated adjusted
close) cannot leak into it, while a bar that was only stored later (a first
ingestion backfills years at once) is still a real bar for its market date."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security
from alpha_lab.database.queries import (
    latest_price_per_date,
    price_history_as_known,
    price_history_as_known_statement,
)
from alpha_lab.strategy import HistoricalScoringService

EVALUATION = date(2024, 12, 31)
FIRST_LOAD = datetime(2024, 12, 31, 20, 0, tzinfo=UTC)  # known on the evaluation date
LATER = datetime(2025, 2, 1, 9, 0, tzinfo=UTC)  # stored after it
BARS = 300


def _naive(moment):
    return moment.replace(tzinfo=None)  # SQLite hands datetimes back without a zone


def _engine():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    return engine


def _bars(session, ticker="PIT", *, ingested=FIRST_LOAD, scale=1.0, bars=BARS, end=EVALUATION):
    start = end - timedelta(days=bars - 1)
    for offset in range(bars):
        value = (10 + offset) * scale
        session.add(Price(ticker=ticker, date=start + timedelta(days=offset), open=value, close=value,
                          adjusted_close=value, volume=1_000, provider="test", ingested_at=ingested))


def _score(engine, evaluation=EVALUATION, tickers=("PIT",)):
    [result] = HistoricalScoringService(engine, load_settings()).score_universe_as_of(
        evaluation, list(tickers), min_score=0, minimum_coverage=0)
    return result


def _seed(*, revision_scale=None, revision_ingested=LATER, first_ingested=FIRST_LOAD):
    engine = _engine()
    with Session(engine) as session:
        session.add(Security(ticker="PIT", sector="Test"))
        _bars(session, ingested=first_ingested)
        if revision_scale is not None:
            _bars(session, ingested=revision_ingested, scale=revision_scale)
        session.commit()
    return engine


# --- the query -----------------------------------------------------------------

def test_a_revision_stored_after_the_bound_is_ignored_and_the_known_version_is_used():
    engine = _seed(revision_scale=10)
    with Session(engine) as session:
        known = price_history_as_known(session, EVALUATION, Price.ticker == "PIT", live=False)
        newest = latest_price_per_date(session, Price.ticker == "PIT")
    assert len(known) == len(newest) == BARS
    assert {row.ingested_at for row in known} == {_naive(FIRST_LOAD)}
    assert {row.ingested_at for row in newest} == {_naive(LATER)}
    engine.dispose()


def test_among_versions_known_by_the_bound_the_newest_wins():
    engine = _engine()
    with Session(engine) as session:
        session.add(Security(ticker="PIT", sector="Test"))
        _bars(session, ingested=datetime(2024, 11, 1, tzinfo=UTC))
        _bars(session, ingested=datetime(2024, 12, 15, tzinfo=UTC), scale=2)
        _bars(session, ingested=LATER, scale=3)
        session.commit()
        rows = price_history_as_known(session, EVALUATION, Price.ticker == "PIT", live=False)
    assert {row.ingested_at for row in rows} == {_naive(datetime(2024, 12, 15, tzinfo=UTC))}
    assert {row.adjusted_close / (10 + (row.date - (EVALUATION - timedelta(days=BARS - 1))).days) for row in rows} == {2}
    engine.dispose()


def test_a_bar_first_stored_after_the_bound_is_still_a_bar_using_its_earliest_version():
    engine = _engine()
    with Session(engine) as session:
        session.add(Security(ticker="PIT", sector="Test"))
        _bars(session, ingested=LATER)  # the whole history first arrives after the evaluation date
        _bars(session, ingested=LATER + timedelta(days=30), scale=5)  # and is revised later still
        session.commit()
        rows = price_history_as_known(session, EVALUATION, Price.ticker == "PIT", live=False)
        strict = list(session.scalars(price_history_as_known_statement(None, Price.ticker == "PIT",
                                                                       Price.ingested_at <= datetime.combine(EVALUATION, datetime.max.time()))))
    assert len(rows) == BARS and {row.ingested_at for row in rows} == {_naive(LATER)}  # earliest version, not the revision
    assert strict == []  # a strict ingestion bound would leave no history at all before the first load
    engine.dispose()


def test_a_live_evaluation_takes_the_newest_version():
    engine = _seed(revision_scale=10)
    with Session(engine) as session:
        rows = price_history_as_known(session, date.today(), Price.ticker == "PIT")
    assert {row.ingested_at for row in rows} == {_naive(LATER)}
    engine.dispose()


# --- the scorer ------------------------------------------------------------------

def test_a_later_revision_cannot_change_a_past_score():
    with_revision, without = _seed(revision_scale=10), _seed()
    revised, plain = _score(with_revision), _score(without)
    assert revised.raw_factors == plain.raw_factors
    assert revised.score == plain.score and revised.percentile_factors == plain.percentile_factors
    with_revision.dispose()
    without.dispose()


def test_the_live_score_still_uses_the_newest_version():
    engine = _seed(revision_scale=10)
    live = _score(engine, evaluation=date.today())
    assert live.raw_factors["last_price"] == (10 + BARS - 1) * 10
    engine.dispose()


def test_history_that_only_arrived_after_the_evaluation_date_still_scores():
    backfilled, loaded_then = _seed(first_ingested=LATER), _seed()
    result, expected = _score(backfilled), _score(loaded_then)
    assert result.raw_factors["last_price"] == expected.raw_factors["last_price"] == 10 + BARS - 1
    assert result.raw_factors == expected.raw_factors and result.score == expected.score
    backfilled.dispose()
    loaded_then.dispose()


def test_one_series_scored_on_two_dates_sees_the_version_held_on_each():
    # A restatement stored between two rebalance dates: the earlier one scores
    # the original prices, the later one the restated prices.
    engine = _seed(revision_scale=10, revision_ingested=datetime(2025, 1, 15, 9, 0, tzinfo=UTC))
    before = _score(engine, evaluation=date(2025, 1, 10))
    after = _score(engine, evaluation=date(2025, 1, 20))
    assert before.raw_factors["last_price"] == 10 + BARS - 1  # the 31 Dec bar as first stored
    assert after.raw_factors["last_price"] == (10 + BARS - 1) * 10  # restated by 15 Jan
    engine.dispose()
