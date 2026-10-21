"""Shared builders for the agent-interface tests: a rich ResearchState built from the
Conviction fixtures, so tools can be exercised without a populated database."""

from conviction_fixtures import _alignment, _research, _state

from alpha_lab.config import load_settings
from alpha_lab.conviction import assess_conviction
from alpha_lab.macro.regime import MacroRegime
from alpha_lab.research_state import ResearchState

EVALUATION = _state(None).evaluation_date


def rich_state(ticker="TST", evaluation_date=EVALUATION, universe_basis=None, **research_kwargs) -> ResearchState:
    state = _state(_research(**research_kwargs), alignment=_alignment(MacroRegime.RISK_OFF), evaluation_date=evaluation_date)
    assessment = assess_conviction(state, coverage=load_settings().coverage, stale_price_days=5)
    value = {"score": 73.2, "percentile": 0.8}
    if universe_basis:
        value["universe"] = {"basis": universe_basis, "size": 12, "snapshot_recorded_at": None}
    scored = state.deterministic_score.model_copy(update={"value": value})
    return state.model_copy(update={
        "deterministic_score": scored,
        "ticker": ticker,
        "conflicts": [c.model_dump(mode="json") for c in assessment.conflicts],
        "conviction": assessment.model_dump(mode="json"),
    })
