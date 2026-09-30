"""Deterministic tests for alpha_lab.research_state (roadmap Phase 5): the
canonical, per-security Research State assembler. See that module's own
docstring for the full design -- these tests exist to lock in the explicit
implementation gate from the approved Phase 4/5 design review:

1. AlignmentService.get_assessment_as_of is a pure read (covered in
   tests/test_alignment_service.py; re-exercised here through the assembler).
2. ResearchField keeps status/observed_at/provenance_id independent.
3. ResearchState is an assembler -- no new persistence table (structural;
   nothing here persists a ResearchState anywhere).
4. get_research_state makes zero network calls and zero writes.
5. A historical evaluation_date never mutates current state.
6. Existing scoring/ranking/backtest behavior is unchanged (covered by the
   full suite + smoke tests remaining green, not by this file alone).
7/8. UI migration and AI wiring are separate, later steps -- not here.
"""
from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from alpha_lab.alignment import AlignmentService
from alpha_lab.calibration import ExternalCalibrationService
from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import (
    AIResearchAnalysis,
    Base,
    NewsArticleRecord,
    Price,
    Security,
)
from alpha_lab.ethics import EthicalClassificationService, load_ethics_policy
from alpha_lab.macro.service import MacroRegimeService
from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.donatien import DonatienCalibration, DonatienFetchResult
from alpha_lab.research_state import ResearchField, get_research_state
from alpha_lab.screener import MarketScreenerService


class _FakeMacroProvider(MarketDataProvider):
    provider_name = "FakeMacroProvider"

    def get_company_info(self, ticker):
        return {"ticker": ticker, "company_name": f"Fixture {ticker}", "asset_type": "INDEX", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        idx = pd.date_range(end=pd.Timestamp(end), periods=60)
        return pd.DataFrame({"close": [50.0] * 60, "high": [50.0] * 60, "low": [50.0] * 60}, index=idx)

    def get_financials(self, ticker):
        return pd.DataFrame()


class _FakeDonatienProvider:
    def __init__(self, *, run_date: date, retrieved_at: datetime):
        self._run_date, self._retrieved_at = run_date, retrieved_at

    def fetch(self) -> DonatienFetchResult:
        calibration = DonatienCalibration(
            run_date=self._run_date, run_time="20:15", macro_report_date=self._run_date,
            dominant_regime="Some prose regime label", confidence="Medium",
            scenario_weights={"Soft Landing": 60, "Reacceleration": 20, "Stagflation": 10, "Deflationary Bust": 10},
            defensiveness=4.0, top_drivers=[], key_changes=[], trend_contrarian_split={}, tiers={},
        )
        return DonatienFetchResult(
            calibration=calibration, retrieved_at=self._retrieved_at,
            content_hash="fixture-hash", source_url="http://fixture", raw_payload={},
        )


def _seed_tracked_security(engine, ticker: str, *, price_date: date) -> None:
    with Session(engine) as session:
        session.add(Security(ticker=ticker, country="US", currency="USD", sector="Technology", is_tracked=True))
        session.add(Price(ticker=ticker, date=price_date, close=10.0, high=11.0, low=9.0,
                           provider="fixture", currency="USD", source="test"))
        session.commit()


def _make(monkeypatch):
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    settings = load_settings()
    return engine, settings


# --- ResearchField: independent status/observed_at/provenance_id ----------


def test_research_field_status_and_observed_at_are_independently_settable():
    """A field can be available (FULL) yet stale (an old observed_at), or
    unavailable (NOT_COMPUTED) regardless of any freshness question --
    status must never be derived from observed_at or vice versa."""
    stale_but_available = ResearchField(status="FULL", observed_at="2020-01-01", value={"x": 1})
    assert stale_but_available.status == "FULL"
    assert stale_but_available.observed_at == "2020-01-01"

    never_computed = ResearchField(status="NOT_COMPUTED")
    assert never_computed.observed_at is None
    assert never_computed.value is None


def test_missing_domain_reports_not_computed_with_no_fabricated_value(monkeypatch):
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "NEW", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()

    state = get_research_state(engine, settings, "NEW")

    assert state.macro.status == "NOT_COMPUTED"
    assert state.macro.value is None
    assert state.donatien.status == "NOT_COMPUTED"
    assert state.donatien_alignment.status == "NOT_COMPUTED"
    assert state.ai_research.status == "NOT_COMPUTED"
    # Ethics is the one exception: EthicalClassificationService.ensure_all
    # auto-classifies every tracked security as a side effect of
    # rebuild_current_research (deterministic, no network -- see that
    # service's own module docstring), so it's already FULL here, not
    # NOT_COMPUTED like the domains above that need an explicit refresh.
    assert state.ethics.status == "FULL"


def test_populated_domains_carry_a_provenance_id(monkeypatch):
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    with Session(engine) as session:
        session.add(NewsArticleRecord(
            ticker="AAA", content_hash="h1", title="AAA news", url="http://x", provider="fixture",
            published_at=datetime.now(UTC), retrieved_at=datetime.now(UTC), raw_payload={},
        ))
        session.commit()
    MacroRegimeService(engine).refresh(_FakeMacroProvider())
    MarketScreenerService(engine, settings).rebuild_current_research()

    state = get_research_state(engine, settings, "AAA")

    assert state.news.status == "FULL"
    assert state.news.provenance_id is not None
    assert state.macro.status == "FULL"
    assert state.macro.provenance_id is not None
    # Macro's provenance_id must be the domain's own real snapshot_id, not
    # a locally re-derived hash -- addressable back to a real persisted row.
    macro_snapshot = MacroRegimeService(engine).get_assessment_as_of(as_of=date.today())
    assert state.macro.provenance_id == macro_snapshot.snapshot_id


def test_repeated_assembly_of_unchanged_evidence_yields_the_same_provenance_id(monkeypatch):
    """provenance_id must be reproducible: re-assembling identical evidence
    (no writes in between) always yields the identical id, for a computed
    (non-snapshot-backed) field."""
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()

    first = get_research_state(engine, settings, "AAA")
    second = get_research_state(engine, settings, "AAA")

    assert first.fundamentals.provenance_id == second.fundamentals.provenance_id
    assert first.deterministic_score.provenance_id == second.deterministic_score.provenance_id


# --- gate 4: zero network calls, zero writes -------------------------------


def test_get_research_state_never_calls_a_provider(monkeypatch):
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()

    def _explode(*args, **kwargs):
        raise AssertionError("get_research_state must never call a provider")

    monkeypatch.setattr("alpha_lab.providers.YFinanceProvider", _explode)
    monkeypatch.setattr("alpha_lab.providers.donatien.DonatienProvider", _explode)

    get_research_state(engine, settings, "AAA")  # must not raise / not construct a provider


def test_get_research_state_writes_nothing_to_the_database(monkeypatch):
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()

    def _table_counts():
        with Session(engine) as session:
            return {
                table.name: len(session.execute(select(table)).fetchall())
                for table in Base.metadata.sorted_tables
            }

    before = _table_counts()
    get_research_state(engine, settings, "AAA")
    get_research_state(engine, settings, "AAA", date.today() - timedelta(days=1))
    after = _table_counts()

    assert before == after


# --- gate 5: a historical evaluation_date never mutates current state -----


def test_historical_evaluation_date_never_mutates_current_alignment(monkeypatch):
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()

    MacroRegimeService(engine).refresh(_FakeMacroProvider(), as_of=date(2024, 6, 1))
    ExternalCalibrationService(engine).refresh(
        _FakeDonatienProvider(run_date=date(2024, 6, 1), retrieved_at=datetime(2024, 6, 1, 10, tzinfo=UTC))
    )
    AlignmentService(engine).refresh(as_of=date(2024, 6, 1))

    MacroRegimeService(engine).refresh(_FakeMacroProvider(), as_of=date(2024, 6, 10))
    ExternalCalibrationService(engine).refresh(
        _FakeDonatienProvider(run_date=date(2024, 6, 10), retrieved_at=datetime(2024, 6, 10, 10, tzinfo=UTC))
    )
    AlignmentService(engine).refresh(as_of=date(2024, 6, 10))

    current_before = AlignmentService(engine).get_current()
    assert current_before.as_of == date(2024, 6, 10)

    # A historical read for the earlier date must never touch "current".
    get_research_state(engine, settings, "AAA", date(2024, 6, 1))

    current_after = AlignmentService(engine).get_current()
    assert current_after.as_of == date(2024, 6, 10)
    assert current_after.content_hash == current_before.content_hash


def test_historical_evaluation_date_never_mutates_current_ethics(monkeypatch):
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()
    policy = load_ethics_policy(settings.ethics_policy_path)
    with Session(engine) as session:
        security = session.get(Security, "AAA")
        EthicalClassificationService(engine, policy).ensure_security(security)

    before = EthicalClassificationService(engine, policy).get_evaluation_as_of(
        "AAA", as_of=date.today()
    )
    get_research_state(engine, settings, "AAA", date.today() - timedelta(days=365))
    after = EthicalClassificationService(engine, policy).get_evaluation_as_of("AAA", as_of=date.today())

    assert before.id == after.id
    assert before.evaluated_at == after.evaluated_at


# --- StockResearch two-path split: today (live) vs. historical (snapshot) -


def test_stock_research_field_is_a_drop_in_replacement_for_get_stock_research(monkeypatch):
    """The one field that makes get_research_state a safe swap-in for an
    existing consumer of ResearchService.get_stock_research (e.g. the
    Company Research page) without losing any of that object's structure."""
    from alpha_lab.research import ResearchService

    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()

    state = get_research_state(engine, settings, "AAA")
    direct = ResearchService(engine, settings).get_stock_research("AAA")

    assert state.stock_research is not None
    # generated_at is a fresh wall-clock stamp on every independent read
    # (not stored state), so exclude it; everything else must match exactly.
    assert state.stock_research.model_dump(exclude={"generated_at"}) == direct.model_dump(exclude={"generated_at"})


def test_today_uses_the_live_current_path_even_without_a_research_snapshot(monkeypatch):
    """A tracked ticker with a live current-research build but no
    ResearchSnapshot ever taken (confirmed against the real database as a
    genuine, common case) must still report today's fundamentals -- the
    live path, not the snapshot path, governs 'today'."""
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()

    state = get_research_state(engine, settings, "AAA")

    assert state.fundamentals.status != "NOT_COMPUTED"


def test_historical_date_with_no_snapshot_is_honestly_not_computed(monkeypatch):
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()

    state = get_research_state(engine, settings, "AAA", date.today() - timedelta(days=365))

    assert state.fundamentals.status == "NOT_COMPUTED"
    assert state.ai_rating.status == "NOT_COMPUTED"


# --- ai_research: the document-based AIResearchAnalysis, PIT-safe --------


def test_ai_research_field_respects_the_pit_upper_bound(monkeypatch):
    engine, settings = _make(monkeypatch)
    _seed_tracked_security(engine, "AAA", price_date=date.today())
    MarketScreenerService(engine, settings).rebuild_current_research()
    with Session(engine) as session:
        session.add(AIResearchAnalysis(
            ticker="AAA", source_document_ids=[1], analyzed_document_ids=[1],
            component_scores={"x": 1.0}, provider="fixture", model="fixture", prompt_version="v1",
            raw_output={}, ai_rating=5.0, confidence=5.0,
            analysis_date=datetime.now(UTC) - timedelta(days=5),
        ))
        session.commit()

    too_early = get_research_state(engine, settings, "AAA", date.today() - timedelta(days=10))
    assert too_early.ai_research.status == "NOT_COMPUTED"

    now = get_research_state(engine, settings, "AAA")
    assert now.ai_research.status == "FULL"


# --- deterministic_score is ranked against the tracked universe ---------------


def _seed_scorable(engine, ticker: str, *, drift: float, tracked: bool = True, days: int = 300) -> None:
    """A security with enough recent, liquid price history to be scored; `drift`
    differentiates the tickers' momentum so their percentile ranks differ."""
    with Session(engine) as session:
        session.add(Security(ticker=ticker, country="US", currency="USD", sector="Technology", is_tracked=tracked))
        price = 100.0
        for offset in range(days, -1, -1):
            price *= 1 + drift
            session.add(Price(
                ticker=ticker, date=date.today() - timedelta(days=offset), close=price, adjusted_close=price,
                high=price * 1.01, low=price * 0.99, volume=1e7, provider="fixture", currency="USD", source="test",
            ))
        session.commit()


def test_deterministic_score_is_ranked_against_the_tracked_universe_not_the_ticker_alone(monkeypatch):
    """Regression: scoring `tickers=[ticker]` ranked a security against itself
    alone, so every ticker scored exactly 100.0."""
    from alpha_lab.strategy import HistoricalScoringService

    engine, settings = _make(monkeypatch)
    for ticker, drift in (("LOW", -0.004), ("MID", 0.001), ("HIGH", 0.006)):
        _seed_scorable(engine, ticker, drift=drift)

    expected = {
        item.ticker: item.score
        for item in HistoricalScoringService(engine, settings).score_universe_as_of(
            date.today(), min_score=0, minimum_coverage=0
        )
    }
    states = {t: get_research_state(engine, settings, t) for t in ("LOW", "MID", "HIGH")}
    scores = {t: state.deterministic_score.value["score"] for t, state in states.items()}

    assert scores == expected
    assert len(set(scores.values())) == 3  # the tickers are distinguished, not all 100.0
    assert scores["HIGH"] > scores["MID"] > scores["LOW"]
    assert all(state.deterministic_score.detail.startswith("ranked against 3 securities") for state in states.values())


def test_an_untracked_ticker_is_ranked_alongside_the_tracked_universe(monkeypatch):
    engine, settings = _make(monkeypatch)
    _seed_scorable(engine, "A", drift=-0.004)
    _seed_scorable(engine, "B", drift=0.006)
    _seed_scorable(engine, "CATALOG", drift=0.001, tracked=False)
    state = get_research_state(engine, settings, "CATALOG")
    assert state.deterministic_score.value is not None
    assert state.deterministic_score.detail.startswith("ranked against 3 securities")


def test_precomputed_universe_scores_are_used_without_rescoring(monkeypatch):
    from alpha_lab.strategy import HistoricalScoringService

    engine, settings = _make(monkeypatch)
    for ticker, drift in (("A", -0.004), ("B", 0.006)):
        _seed_scorable(engine, ticker, drift=drift)
    universe_scores = HistoricalScoringService(engine, settings).score_universe_as_of(
        date.today(), min_score=0, minimum_coverage=0
    )

    def _forbidden(*args, **kwargs):
        raise AssertionError("must not rescore when universe_scores is supplied")

    monkeypatch.setattr(HistoricalScoringService, "score_universe_as_of", _forbidden)
    state = get_research_state(engine, settings, "B", universe_scores=universe_scores)
    assert state.deterministic_score.value["score"] == next(i.score for i in universe_scores if i.ticker == "B")
