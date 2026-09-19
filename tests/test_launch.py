"""Deterministic tests for scripts/launch.py's contract: launch Streamlit
regardless of the core refresh's outcome, including an unexpected
(non-ProviderError, non-rebuild) failure -- see alpha_lab.refresh's module
docstring for why run_core_refresh itself only isolates those two failure
modes, and this script's own broader "launch regardless" promise is kept
by an explicit wrapper around the call, not by run_core_refresh alone.

Loads the script as a module (it is a script, not part of the alpha_lab
package) so main() can be called directly with os.execvp/sys.argv/the
refresh functions monkeypatched -- never actually replacing the process
or touching a real database/provider.
"""

from datetime import date
from pathlib import Path
import importlib.util

from alpha_lab.database import create_schema, make_engine
from alpha_lab.refresh import CoreRefreshResult

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "launch.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("launch_under_test", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare(tmp_path, monkeypatch):
    db_path = tmp_path / "launch.db"
    engine = make_engine(f"sqlite:///{db_path}")
    create_schema(engine)
    engine.dispose()
    monkeypatch.setenv("ALPHALAB_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr("sys.argv", ["launch.py"])
    return _load_script_module()


def test_launch_execs_streamlit_when_data_is_fresh(tmp_path, monkeypatch):
    module = _prepare(tmp_path, monkeypatch)
    exec_calls = []
    monkeypatch.setattr(module, "is_universe_price_stale", lambda engine, days: False)
    monkeypatch.setattr(module.os, "execvp", lambda *args: exec_calls.append(args))

    module.main()

    assert len(exec_calls) == 1


def test_launch_still_execs_streamlit_after_a_normal_refresh(tmp_path, monkeypatch):
    module = _prepare(tmp_path, monkeypatch)
    exec_calls = []
    monkeypatch.setattr(module, "is_universe_price_stale", lambda engine, days: True)
    monkeypatch.setattr(
        module, "run_core_refresh",
        lambda engine, settings: CoreRefreshResult(
            tickers_attempted=["NVDA"], tickers_succeeded=["NVDA"],
            research_rebuilt=True, research_record_count=1,
        ),
    )
    monkeypatch.setattr(module.os, "execvp", lambda *args: exec_calls.append(args))

    module.main()

    assert len(exec_calls) == 1


def test_launch_still_execs_streamlit_when_core_refresh_raises_unexpectedly(tmp_path, monkeypatch, capsys):
    """The bug this guards: run_core_refresh only isolates a per-ticker
    ProviderError or a rebuild failure (both land on CoreRefreshResult) --
    an infrastructure-level failure outside those paths previously
    propagated straight out of main(), so os.execvp (and therefore the
    dashboard) was never reached at all."""
    module = _prepare(tmp_path, monkeypatch)
    exec_calls = []
    monkeypatch.setattr(module, "is_universe_price_stale", lambda engine, days: True)

    def _explode(engine, settings):
        raise RuntimeError("simulated infrastructure failure")

    monkeypatch.setattr(module, "run_core_refresh", _explode)
    monkeypatch.setattr(module.os, "execvp", lambda *args: exec_calls.append(args))

    module.main()  # must not raise

    assert len(exec_calls) == 1
    assert "simulated infrastructure failure" in capsys.readouterr().out


def test_launch_still_execs_streamlit_when_the_rebuild_step_fails(tmp_path, monkeypatch):
    module = _prepare(tmp_path, monkeypatch)
    exec_calls = []
    monkeypatch.setattr(module, "is_universe_price_stale", lambda engine, days: True)
    monkeypatch.setattr(
        module, "run_core_refresh",
        lambda engine, settings: CoreRefreshResult(
            tickers_attempted=["NVDA"], tickers_succeeded=["NVDA"],
            research_rebuilt=False, research_error="simulated rebuild failure",
        ),
    )
    monkeypatch.setattr(module.os, "execvp", lambda *args: exec_calls.append(args))

    module.main()

    assert len(exec_calls) == 1
