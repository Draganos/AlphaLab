"""Regression tests for app/dashboard/main.py's staleness banner and its two
refresh triggers (the automatic once-per-session refresh of whatever is
currently stale, and the manual Full Refresh button).

Bug found by actually clicking the Full Refresh button (not just
unit-testing run_core_refresh in isolation): the page computed and
rendered its "price data is stale" banner BEFORE the button's own refresh
logic ran later in that same script execution, so a successful Full
Refresh click still showed the "stale" warning above its own "Ingested
.../research rebuilt" success message in that identical page render --
contradicting itself. The underlying is_universe_price_stale() computation
was always correct (a *subsequent* rerun always showed the caption
correctly); the bug was purely that the banner never got the chance to
reflect what a refresh in the same run had just done. Fixed via a
re-rendered st.empty() placeholder.

Uses streamlit.testing.v1.AppTest rather than a plain importlib module exec
(as tests/test_dashboard_main_screener.py does) because both this bug and
the automatic-refresh feature are only observable through an actual
simulated button click / a real st.session_state across the in-run
rendering order -- outside a live ScriptRunContext, st.button() always
returns False and st.session_state does not persist meaningfully, so plain
module exec can never exercise either path.
"""

from datetime import date, timedelta

import pandas as pd
from streamlit.testing.v1 import AppTest

from alpha_lab.database import create_schema, make_engine
from alpha_lab.database.models import Price, Security
from alpha_lab.providers.base import MarketDataProvider
from sqlalchemy.orm import Session

_MAIN_PATH = "app/dashboard/main.py"


class _FakeProvider(MarketDataProvider):
    """Succeeds for every ticker; tracks every ticker get_price_history was
    actually called for, mirroring tests/test_refresh.py's own fake."""

    provider_name = "FakeTest"

    def __init__(self):
        self.calls: list[str] = []

    def get_company_info(self, ticker):
        return {"ticker": ticker, "company_name": f"{ticker} Inc", "country": "US", "currency": "USD"}

    def get_price_history(self, ticker, start, end):
        self.calls.append(ticker)
        return pd.DataFrame(
            {"close": [123.45], "adjusted_close": [123.45]},
            index=pd.to_datetime([str(date.today())]),
        )

    def get_financials(self, ticker):
        return pd.DataFrame()


def _seed_security(db_path, ticker: str, price_date: date | None) -> None:
    """price_date=None seeds a tracked security with no price rows at all
    (also stale, per is_universe_price_stale's own docstring)."""
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    with Session(engine) as session:
        session.add(Security(ticker=ticker, country="US", currency="USD"))
        if price_date is not None:
            session.add(Price(
                ticker=ticker, date=price_date, close=100.0,
                high=101.0, low=99.0, provider="fixture", currency="USD", source="test",
            ))
        session.commit()
    engine.dispose()


def test_a_successful_full_refresh_clears_the_stale_banner_in_the_same_run(tmp_path, monkeypatch):
    """The manual Full Refresh button's own banner-ordering fix, isolated
    from the automatic on-session-start trigger by pre-seeding session
    state as though that automatic attempt already happened (and left the
    data stale, e.g. because it isn't wired up in this scenario) -- this
    test is specifically about the button's own code path."""
    db_path = tmp_path / "dashboard.db"
    _seed_security(db_path, "NVDA", date.today() - timedelta(days=30))
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    fake = _FakeProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)

    at = AppTest.from_file(_MAIN_PATH)
    at.session_state["auto_stale_refresh_attempted"] = True
    at.run(timeout=60)
    assert not at.exception
    assert any("older than the configured" in warning.value for warning in at.warning)
    assert fake.calls == []  # the automatic trigger must not have run

    at.button[0].click().run(timeout=120)
    assert not at.exception
    assert any("research rebuilt" in success.value for success in at.success)
    # The stale warning must not still be showing in this exact same run --
    # the refresh that just succeeded fixed the very thing it warns about.
    assert not any("older than the configured" in warning.value for warning in at.warning)
    assert any("within the configured" in caption.value for caption in at.caption)


def test_stale_data_is_automatically_refreshed_once_on_session_start(tmp_path, monkeypatch):
    """The new automatic trigger: a stale ticker gets refreshed without any
    button click, within the very first run, and the fix is visible in
    that same run (not just on a later rerun)."""
    db_path = tmp_path / "dashboard.db"
    _seed_security(db_path, "NVDA", date.today() - timedelta(days=30))
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    fake = _FakeProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)

    at = AppTest.from_file(_MAIN_PATH)
    at.run(timeout=60)

    assert not at.exception
    assert fake.calls == ["NVDA"]
    assert not any("older than the configured" in warning.value for warning in at.warning)
    assert any("Automatically refreshed" in caption.value for caption in at.caption)
    assert any("within the configured" in caption.value for caption in at.caption)
    assert at.session_state["auto_stale_refresh_attempted"] is True

    # A later rerun (e.g. any widget interaction) must not trigger a second
    # automatic attempt -- once per session only.
    at.run(timeout=60)
    assert fake.calls == ["NVDA"]


def test_automatic_refresh_only_ingests_the_stale_subset_not_the_full_universe(tmp_path, monkeypatch):
    """The automatic trigger must cost proportionally to what's actually
    stale -- a fresh ticker sitting alongside a stale one must never be
    re-ingested just because something else in the universe needed it."""
    db_path = tmp_path / "dashboard.db"
    _seed_security(db_path, "NVDA", date.today() - timedelta(days=30))  # stale
    engine = make_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        session.add(Security(ticker="AAPL", country="US", currency="USD"))
        session.add(Price(
            ticker="AAPL", date=date.today(), close=200.0,
            high=201.0, low=199.0, provider="fixture", currency="USD", source="test",
        ))
        session.commit()
    engine.dispose()
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    fake = _FakeProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)

    at = AppTest.from_file(_MAIN_PATH)
    at.run(timeout=60)

    assert not at.exception
    assert fake.calls == ["NVDA"]  # AAPL was already fresh -- never touched


def test_a_fully_fresh_universe_never_triggers_the_automatic_refresh(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    _seed_security(db_path, "NVDA", date.today())
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    fake = _FakeProvider()
    monkeypatch.setattr("alpha_lab.refresh.YFinanceProvider", lambda: fake)

    at = AppTest.from_file(_MAIN_PATH)
    at.run(timeout=60)

    assert not at.exception
    assert fake.calls == []
    assert at.session_state["auto_stale_refresh_attempted"] is True
