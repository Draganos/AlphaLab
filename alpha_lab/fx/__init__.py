"""Point-in-time currency conversion to USD.

Deliberately separate from `alpha_lab.scorecard`/`alpha_lab.search.screening`:
neither does I/O of its own (see their own module docstrings/`classify_tier`'s
"no database read" contract), so a security's raw, native-currency
`market_cap` is converted to its USD equivalent once, here, at the
canonical valuation layer (`alpha_lab.screener.service.MarketScreenerService`),
before either of those pure functions ever sees it.
"""

from alpha_lab.fx.service import FXRateService

__all__ = ["FXRateService"]
