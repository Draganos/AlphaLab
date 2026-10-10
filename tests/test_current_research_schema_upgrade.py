"""A persisted research build written before `LiveResearchRecord` gained
`currency` / `market_cap_usd` (FX support) still validates -- the new fields
default to None -- but None there means "never computed", so a USD
large-cap read as unconvertible and was tiered SPECULATIVE. The fix is one
explicit rebuild, not guessing USD from the old native `market_cap` (a legacy
record has no `currency`, so a JPY cap would be mis-tiered as huge)."""


import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from alpha_lab.config import load_settings
from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import CurrentResearchSnapshot, Security
from alpha_lab.refresh import upgrade_stale_current_research
from alpha_lab.scorecard.verdict import SecurityTier, classify_tier
from alpha_lab.screener import MarketScreenerService

_NEW_FIELDS = ("market_cap_usd", "currency", "fund_aum", "fund_category")


@pytest.fixture(autouse=True)
def _fresh_upgrade_memory():
    """The upgrade remembers failed/skipped attempts per (database, build id);
    every test's in-memory database reuses build id 1, so reset between tests."""
    import alpha_lab.refresh as refresh_module

    refresh_module._UPGRADE_OUTCOMES.clear()
    yield
    refresh_module._UPGRADE_OUTCOMES.clear()


def _engine_with_build(*securities: Security, url: str = "sqlite:///:memory:"):
    engine = make_engine(url)
    create_schema(engine)
    with Session(engine) as session:
        session.add_all(securities)
        session.commit()
    MarketScreenerService(engine, load_settings()).rebuild_current_research()
    return engine


def _make_build_legacy(engine) -> None:
    """Rewrite the stored payloads as the pre-FX schema wrote them."""
    with Session(engine) as session:
        for row in session.scalars(select(CurrentResearchSnapshot)).all():
            session.execute(
                update(CurrentResearchSnapshot)
                .where(CurrentResearchSnapshot.id == row.id)
                .values(payload={k: v for k, v in row.payload.items() if k not in _NEW_FIELDS})
            )
        session.commit()


def _big_usd() -> Security:
    return Security(ticker="BIGUSD", market_cap=50_000_000_000.0, currency="USD", is_tracked=True)


def test_a_legacy_build_misreads_a_usd_large_cap_as_speculative_until_upgraded():
    settings = load_settings()
    engine = _engine_with_build(_big_usd())
    screener = MarketScreenerService(engine, settings)
    assert screener.current_research_schema_is_stale() is False

    _make_build_legacy(engine)
    assert screener.current_research_schema_is_stale() is True
    legacy = screener.read_current_record("BIGUSD")
    assert legacy.market_cap == 50_000_000_000.0 and legacy.market_cap_usd is None
    assert classify_tier(legacy.market_cap_usd) is SecurityTier.SPECULATIVE  # the bug

    result = upgrade_stale_current_research(engine, settings)

    assert result.rebuilt is True and result.message
    assert screener.current_research_schema_is_stale() is False
    upgraded = screener.read_current_record("BIGUSD")
    assert upgraded.market_cap_usd == 50_000_000_000.0
    assert classify_tier(upgraded.market_cap_usd) is SecurityTier.CORE


def test_a_legacy_non_usd_cap_is_never_guessed_as_usd():
    """Why the upgrade rebuilds from the database instead of copying the old
    `market_cap` into `market_cap_usd`: that would call 50bn JPY ~50bn USD."""
    settings = load_settings()
    engine = _engine_with_build(
        Security(ticker="YEN", market_cap=50_000_000_000.0, currency="JPY", is_tracked=True)
    )
    _make_build_legacy(engine)

    upgrade_stale_current_research(engine, settings)

    record = MarketScreenerService(engine, settings).read_current_record("YEN")
    assert record.currency == "JPY" and record.market_cap_usd is None  # no rate ingested: not fabricated


def test_a_current_build_is_left_alone(monkeypatch):
    settings = load_settings()
    engine = _engine_with_build(_big_usd())
    monkeypatch.setattr(
        MarketScreenerService, "rebuild_current_research",
        lambda self: (_ for _ in ()).throw(AssertionError("must not rebuild a current build")),
    )
    result = upgrade_stale_current_research(engine, settings)
    assert result.rebuilt is False and result.message is None


def test_no_build_at_all_is_not_stale():
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    assert MarketScreenerService(engine, load_settings()).current_research_schema_is_stale() is False
    assert upgrade_stale_current_research(engine, load_settings()).message is None


def test_an_oversized_universe_is_not_rebuilt_during_a_page_load(monkeypatch):
    import alpha_lab.refresh as refresh_module

    settings = load_settings()
    engine = _engine_with_build(_big_usd())
    _make_build_legacy(engine)
    monkeypatch.setattr(refresh_module, "MAX_FULL_UNIVERSE_REFRESH_BATCH", 0)
    monkeypatch.setattr(
        MarketScreenerService, "rebuild_current_research",
        lambda self: (_ for _ in ()).throw(AssertionError("must not rebuild")),
    )

    result = upgrade_stale_current_research(engine, settings)

    assert result.rebuilt is False and result.level == "warning"
    assert "rebuild_research.py" in result.message


def test_a_failing_rebuild_is_reported_not_raised(monkeypatch):
    settings = load_settings()
    engine = _engine_with_build(_big_usd())
    _make_build_legacy(engine)
    monkeypatch.setattr(
        MarketScreenerService, "rebuild_current_research",
        lambda self: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    result = upgrade_stale_current_research(engine, settings)

    assert result.rebuilt is False and result.level == "error" and "disk full" in result.message


def test_the_dashboard_upgrades_a_legacy_build_on_load(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    db_path = tmp_path / "legacy.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    with Session(engine) as session:
        session.add(_big_usd())
        session.commit()
    MarketScreenerService(engine, load_settings()).rebuild_current_research()
    _make_build_legacy(engine)
    engine.dispose()
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")

    at = AppTest.from_file("app/dashboard/main.py")
    at.session_state["auto_stale_refresh_attempted"] = True
    at.run(timeout=60)

    assert not at.exception
    assert any("rebuilt once automatically" in info.value for info in at.info)
    engine = make_engine(f"sqlite:///{db_path}")
    assert MarketScreenerService(engine, load_settings()).current_research_schema_is_stale() is False
    engine.dispose()


def test_a_failed_upgrade_is_not_retried_on_every_rerun(monkeypatch):
    """Review finding: the build stays stale after a failure, and the upgrade
    runs on every Streamlit rerun of three pages -- each one repeating the same
    expensive failing rebuild."""
    settings = load_settings()
    engine = _engine_with_build(_big_usd())
    _make_build_legacy(engine)
    calls = []

    def failing(self):
        calls.append(1)
        raise RuntimeError("provider down")

    monkeypatch.setattr(MarketScreenerService, "rebuild_current_research", failing)
    first = upgrade_stale_current_research(engine, settings)
    second = upgrade_stale_current_research(engine, settings)
    third = upgrade_stale_current_research(engine, settings)
    assert len(calls) == 1 and first.level == second.level == third.level == "error"
    assert "provider down" in third.message and "rebuild_research.py" in third.message


def test_a_new_build_lets_the_upgrade_try_again(monkeypatch):
    settings = load_settings()
    engine = _engine_with_build(_big_usd())
    _make_build_legacy(engine)
    monkeypatch.setattr(
        MarketScreenerService, "rebuild_current_research",
        lambda self: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    assert upgrade_stale_current_research(engine, settings).level == "error"
    monkeypatch.undo()  # the cause is fixed; an explicit rebuild persists a new, current build
    MarketScreenerService(engine, settings).rebuild_current_research()
    assert MarketScreenerService(engine, settings).current_research_schema_is_stale() is False
    assert upgrade_stale_current_research(engine, settings).message is None


def test_an_empty_universe_is_reported_once_not_rebuilt_every_rerun(monkeypatch):
    settings = load_settings()
    engine = _engine_with_build(_big_usd())
    _make_build_legacy(engine)
    with Session(engine) as session:
        session.query(Security).update({"is_tracked": False})
        session.commit()
    calls = []
    original = MarketScreenerService.rebuild_current_research

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(MarketScreenerService, "rebuild_current_research", counting)
    results = [upgrade_stale_current_research(engine, settings) for _ in range(3)]
    assert len(calls) == 1 and all(r.level == "warning" and "no tracked" in r.message for r in results)


def test_a_paid_ai_provider_is_never_triggered_by_a_page_load(monkeypatch):
    """Review finding: rebuild_current_research runs AIResearchService.
    ensure_all(); with a paid provider configured, opening a page must not
    spend money."""
    import alpha_lab.refresh as refresh_module

    settings = load_settings()
    engine = _engine_with_build(_big_usd())
    _make_build_legacy(engine)
    monkeypatch.setattr(refresh_module, "_ai_provider_may_incur_cost", lambda: True)
    monkeypatch.setattr(
        MarketScreenerService, "rebuild_current_research",
        lambda self: (_ for _ in ()).throw(AssertionError("must not rebuild with a paid AI provider")),
    )
    result = upgrade_stale_current_research(engine, settings)
    assert result.rebuilt is False and result.level == "warning"
    assert "paid AI" in result.message and "rebuild_research.py" in result.message


def test_the_paid_provider_check_recognises_the_openai_provider_only(monkeypatch):
    import alpha_lab.refresh as refresh_module
    from alpha_lab.ai.research import OpenAIResearchProvider

    monkeypatch.delenv("ALPHALAB_AI_PROVIDER", raising=False)
    assert refresh_module._ai_provider_may_incur_cost() is False  # default: local rule-based provider
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "disabled")
    assert refresh_module._ai_provider_may_incur_cost() is False
    monkeypatch.setenv("ALPHALAB_AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert isinstance(refresh_module.configured_ai_research_provider(), OpenAIResearchProvider)
    assert refresh_module._ai_provider_may_incur_cost() is True


def test_concurrent_page_loads_rebuild_only_once(monkeypatch, tmp_path):
    import threading

    settings = load_settings()
    # file-backed: an in-memory SQLite database is private to each thread
    engine = _engine_with_build(_big_usd(), url=f"sqlite:///{tmp_path / 'concurrent.db'}")
    _make_build_legacy(engine)
    calls, original = [], MarketScreenerService.rebuild_current_research

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(MarketScreenerService, "rebuild_current_research", counting)
    threads = [threading.Thread(target=upgrade_stale_current_research, args=(engine, settings)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert len(calls) == 1
    assert MarketScreenerService(engine, settings).current_research_schema_is_stale() is False
