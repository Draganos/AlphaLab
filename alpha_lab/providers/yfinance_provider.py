"""Optional US data provider. Missing fields remain null.

External call failures (rate limiting, network/DNS unavailability, an
unclassified provider error) are never treated as data -- they raise
``ProviderError`` for the caller to handle, instead of surfacing raw
yfinance/curl_cffi/requests internals or being silently swallowed into a
zero, an empty frame, or a fabricated value.
"""

from datetime import UTC, date, datetime
from typing import Any
import calendar
import math
import pandas as pd

from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import (
    ProviderError,
    ProviderErrorKind,
    call_with_classification,
)
from alpha_lab.providers.interfaces import (
    AnalystEventProvider,
    EstimateProvider,
    FXRateProvider,
    ResearchNewsProvider,
)
from alpha_lab.providers.ticker_notation import to_hyphenated_symbol


class YFinanceProvider(
    MarketDataProvider,
    ResearchNewsProvider,
    EstimateProvider,
    AnalystEventProvider,
    FXRateProvider,
):
    def _ticker(self, symbol: str):
        import yfinance as yf

        return yf.Ticker(_yahoo_symbol(symbol))

    def get_price_history(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        frame = call_with_classification(
            lambda: self._ticker(ticker).history(start=start, end=end, auto_adjust=False),
            provider=self.provider_name,
        )
        if frame.empty:
            return frame
        frame = frame.rename(columns={"Adj Close": "adjusted_close"})
        frame.columns = [
            str(column).lower().replace(" ", "_") for column in frame.columns
        ]
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
        return frame[
            [
                c
                for c in ["open", "high", "low", "close", "adjusted_close", "volume"]
                if c in frame
            ]
        ]

    def get_fx_rate_history(
        self, currency: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Daily `currency` -> USD spot rate from Yahoo Finance's own
        `<CUR>USD=X` FX pair (e.g. `AEDUSD=X`), using each bar's Close.
        USD is never queried here -- there is no `USDUSD=X` pair, and 1 USD
        is definitionally 1 USD, so callers should never call this for
        currency == "USD" in the first place (see
        `alpha_lab.fx.FXRateService`, which enforces exactly that).

        A currency Yahoo Finance has no FX pair for returns an empty list,
        exactly like `get_price_history` already returns an empty frame for
        an unrecognized ticker -- never an error, since "no data for this
        symbol" is a normal, expected outcome here, not a provider failure."""
        import yfinance as yf

        symbol = currency.strip().upper()
        frame = call_with_classification(
            lambda: yf.Ticker(f"{symbol}USD=X").history(
                start=start, end=end, auto_adjust=False
            ),
            provider=self.provider_name,
        )
        if frame.empty:
            return []
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
        return [
            {"date": index.date(), "rate_to_usd": float(row["Close"])}
            for index, row in frame.iterrows()
            if row.get("Close") is not None and math.isfinite(row["Close"])
        ]

    def get_company_info(self, ticker: str) -> dict[str, Any]:
        info = call_with_classification(
            lambda: self._ticker(ticker).get_info(),
            provider=self.provider_name,
        )
        return {
            "ticker": ticker.upper(),
            "company_name": info.get("longName"),
            "exchange": info.get("exchange"),
            "country": info.get("country"),
            "sector": info.get("sector"),
            "currency": info.get("currency"),
            "industry": info.get("industry"),
            "asset_type": info.get("quoteType"),
            "market_cap": _number(info.get("marketCap"), positive=True),
            "business_description": info.get("longBusinessSummary"),
            "metadata_provider": self.provider_name,
            "metadata_source": "yfinance quoteSummary",
        }

    def get_financials(self, ticker: str) -> pd.DataFrame:
        ticker_obj = self._ticker(ticker)
        statement = call_with_classification(
            lambda: ticker_obj.quarterly_income_stmt,
            provider=self.provider_name,
        )
        cashflow = call_with_classification(
            lambda: ticker_obj.quarterly_cashflow,
            provider=self.provider_name,
        )
        balance = call_with_classification(
            lambda: ticker_obj.quarterly_balance_sheet,
            provider=self.provider_name,
        )
        if statement.empty:
            return pd.DataFrame()
        rows: list[dict[str, Any]] = []
        for period in statement.columns:

            def value(frame: pd.DataFrame, label: str) -> float | None:
                if (
                    label not in frame.index
                    or period not in frame.columns
                    or pd.isna(frame.at[label, period])
                ):
                    return None
                return _number(frame.at[label, period])

            rows.append(
                {
                    "period": pd.Timestamp(period).date(),
                    "publication_date": None,
                    "revenue": value(statement, "Total Revenue"),
                    "ebitda": value(statement, "EBITDA"),
                    "ebit": value(statement, "EBIT"),
                    "net_income": value(statement, "Net Income"),
                    "eps": value(statement, "Diluted EPS"),
                    "free_cash_flow": value(cashflow, "Free Cash Flow"),
                    "total_debt": value(balance, "Total Debt"),
                    "cash": value(
                        balance, "Cash Cash Equivalents And Short Term Investments"
                    ),
                    "total_equity": value(balance, "Stockholders Equity"),
                    "shares_outstanding": value(balance, "Ordinary Shares Number"),
                    "gross_profit": value(statement, "Gross Profit"),
                    "total_assets": value(balance, "Total Assets"),
                    "current_assets": value(balance, "Current Assets"),
                    "current_liabilities": value(balance, "Current Liabilities"),
                    "interest_expense": value(statement, "Interest Expense"),
                    "dividends_paid": value(cashflow, "Cash Dividends Paid"),
                    "share_repurchases": value(cashflow, "Repurchase Of Capital Stock"),
                }
            )
        return pd.DataFrame(rows)

    def get_analyst_consensus(self, ticker: str) -> dict[str, Any]:
        """Raw analyst recommendation counts + price targets for `ticker`.

        Returns a plain dict of raw inputs (not the canonical
        ``AnalystConsensus`` model) -- consistent with ``get_company_info``'s
        existing style. ``alpha_lab.research.analyst_consensus.build_analyst_consensus``
        turns this into the canonical, rated object; that mapping is kept
        pure and provider-independent so it can be unit-tested without a
        provider at all.

        Only the current ("0m") recommendationTrend row is used -- the
        other rows the endpoint returns are trailing months of the same
        trend, which is Analyst *Revisions* territory (out of scope here;
        see the module docstring in analyst_consensus.py).

        `as_of` is today's date (when AlphaLab made this call) -- Yahoo's
        recommendationTrend/financialData modules do not expose a
        provider-side observation timestamp, so this is never presented as
        one.
        """
        ticker_obj = self._ticker(ticker)
        recommendations = call_with_classification(
            lambda: ticker_obj.get_recommendations(),
            provider=self.provider_name,
        )
        targets = call_with_classification(
            lambda: ticker_obj.get_analyst_price_targets(),
            provider=self.provider_name,
        )
        counts = _current_recommendation_counts(recommendations)
        return {
            "ticker": ticker.upper(),
            "as_of": date.today(),
            "strong_buy": counts.get("strongBuy"),
            "buy": counts.get("buy"),
            "hold": counts.get("hold"),
            "sell": counts.get("sell"),
            "strong_sell": counts.get("strongSell"),
            "target_current": _number(targets.get("current"), positive=True),
            "target_low": _number(targets.get("low"), positive=True),
            "target_mean": _number(targets.get("mean"), positive=True),
            "target_median": _number(targets.get("median"), positive=True),
            "target_high": _number(targets.get("high"), positive=True),
            "source": self.provider_name,
        }

    def get_fund_data(self, ticker: str) -> dict[str, Any] | None:
        """Raw fund-specific evidence for `ticker` from yfinance's
        dedicated ``Ticker.funds_data`` accessor: fund identity/category,
        expense ratio/holdings turnover/total net assets (AUM), asset-class
        mix, sector weights, top holdings, and equity-holdings valuation
        averages (P/E, P/B, P/S, P/CF).

        Returns ``None`` when `ticker` genuinely has no fund data --
        yfinance raises ``YFDataException`` for a plain equity (e.g.
        "NVDA: No Fund data found."), classified as ``ProviderErrorKind.
        NO_DATA`` by ``classify_yfinance_error``, which is a legitimate
        "not a fund" outcome here, never a provider failure. Any other
        classification (rate limiting, network, unknown) still raises
        ``ProviderError`` normally.

        Deliberately excludes, verified live against the installed
        yfinance version:

        * ``bond_holdings``/``bond_ratings`` -- unreliable placeholders for
          a pure-equity fund (FTEC/GDX each return a single degenerate
          ``{"us_government": 0.0}`` bond-rating entry rather than an
          absent field), and even a genuine bond fund's own
          ``bond_holdings`` leaves ``Duration``/``Credit Quality``
          unpopulated. AlphaLab's current universe has no bond funds, so
          implementing these now would mean guessing at reliability rather
          than confirming it.
        * ``equity_holdings``' ``Median Market Cap``/``3 Year Earnings
          Growth`` rows -- observed unpopulated (``<NA>``) for every fund
          checked.
        * ``info`` dict fields such as ``dividendYield``/``ytdReturn``
          (distribution/performance evidence) -- observed inconsistent in
          this yfinance version (FTEC's ``dividendYield`` read 0.35 while
          its own ``yield`` field read 0.0035 for the same fund on the same
          call).

        Returns a plain dict of raw inputs (not the canonical
        ``FundEvidence`` model), consistent with ``get_analyst_consensus``'s
        existing style; ``alpha_lab.research.fund_evidence.build_fund_evidence``
        turns this into the canonical object.
        """
        ticker_obj = self._ticker(ticker)

        def _fetch() -> dict[str, Any]:
            # FundsData is a lazy scraper: accessing `.funds_data` itself
            # never raises -- the actual fetch (and YFDataException for a
            # non-fund ticker) happens on first sub-property access. Every
            # property must therefore be read inside this one
            # call_with_classification call, not just the initial
            # `.funds_data` access, or a non-fund ticker's exception would
            # escape unclassified.
            funds_data = ticker_obj.funds_data
            overview = funds_data.fund_overview or {}
            asset_classes = funds_data.asset_classes or {}
            operations = funds_data.fund_operations
            equity_holdings = funds_data.equity_holdings
            top_holdings = funds_data.top_holdings
            return {
                "ticker": ticker.upper(),
                "as_of": date.today(),
                "category_name": overview.get("categoryName"),
                "fund_family": overview.get("family"),
                "legal_type": overview.get("legalType"),
                "description": _text(funds_data.description),
                "cash_position": _number(asset_classes.get("cashPosition")),
                "stock_position": _number(asset_classes.get("stockPosition")),
                "bond_position": _number(asset_classes.get("bondPosition")),
                "preferred_position": _number(asset_classes.get("preferredPosition")),
                "convertible_position": _number(asset_classes.get("convertiblePosition")),
                "other_position": _number(asset_classes.get("otherPosition")),
                "sector_weightings": {
                    str(sector): _number(weight)
                    for sector, weight in (funds_data.sector_weightings or {}).items()
                },
                "expense_ratio": _fund_table_value(operations, "Annual Report Expense Ratio", 0),
                "category_avg_expense_ratio": _fund_table_value(
                    operations, "Annual Report Expense Ratio", 1
                ),
                "holdings_turnover": _fund_table_value(operations, "Annual Holdings Turnover", 0),
                "total_net_assets": _fund_table_value(operations, "Total Net Assets", 0),
                "price_earnings": _fund_table_value(equity_holdings, "Price/Earnings", 0),
                "price_book": _fund_table_value(equity_holdings, "Price/Book", 0),
                "price_sales": _fund_table_value(equity_holdings, "Price/Sales", 0),
                "price_cashflow": _fund_table_value(equity_holdings, "Price/Cashflow", 0),
                "top_holdings": _top_holdings_rows(top_holdings),
                "source": self.provider_name,
            }

        try:
            return call_with_classification(_fetch, provider=self.provider_name)
        except ProviderError as error:
            if error.kind == ProviderErrorKind.NO_DATA:
                return None
            raise

    def get_estimates(self, ticker: str, observation_date: date) -> list[dict[str, Any]]:
        """Current consensus EPS/revenue estimates for `ticker`, as observed
        on `observation_date` (always AlphaLab's own retrieval date, exactly
        like ``get_analyst_consensus``'s ``as_of`` -- never a historical
        reconstruction). Returns one dict per fiscal period yfinance reports
        a usable estimate for; genuinely no analyst estimate coverage for
        this ticker (confirmed live for ETFs: yfinance returns an empty
        frame, not an error) returns an empty list, never fabricated rows.

        Callers persist each call's result via
        ``alpha_lab.ingestion.estimates.snapshot_estimates``, which content-
        hash-dedupes so re-running this on an unchanged consensus is a
        no-op -- revision history only ever accumulates from genuinely
        distinct future calls, never backfilled from today's data.

        yfinance exposes four relative periods ("0q"/"+1q" current/next
        quarter, "0y"/"+1y" current/next fiscal year) but never an exact
        fiscal-period-end date for the quarterly ones. Only "0y"/"+1y" are
        captured here: "0y" uses this company's own ``nextFiscalYearEnd``
        (from ``get_info()``) directly, and "+1y" is exactly 12 months
        after it -- both precise, since a fiscal year is unambiguously 12
        months and adding exactly 12 months never crosses into a
        differently-sized month. The quarterly labels were deliberately
        investigated and dropped: deriving their end date would mean adding
        3/6 months to the last-reported-quarter-end, and for a company whose
        quarters end on a calendar month boundary (e.g. Dec 31) while an
        intermediate quarter ends on a shorter month (e.g. Jun 30), naive
        month-arithmetic can land one day off the true quarter-end --
        confirmed against real AAL/MA data during Phase 2H's investigation.
        Rather than persist a `fiscal_period` that could be a fabricated
        day off from the truth, the quarterly estimates are not captured at
        all until a source of their exact date exists.
        """
        ticker_obj = self._ticker(ticker)
        eps_frame = call_with_classification(
            lambda: ticker_obj.get_earnings_estimate(), provider=self.provider_name
        )
        if eps_frame is None or eps_frame.empty:
            return []
        revenue_frame = call_with_classification(
            lambda: ticker_obj.get_revenue_estimate(), provider=self.provider_name
        )
        info = call_with_classification(
            lambda: ticker_obj.get_info(), provider=self.provider_name
        )
        anchors = _fiscal_anchors(info)

        observations: list[dict[str, Any]] = []
        for period_label, row in eps_frame.iterrows():
            fiscal_period = _fiscal_period_for(str(period_label), anchors)
            if fiscal_period is None:
                continue
            consensus_eps = _number(row.get("avg"))
            if consensus_eps is None:
                continue
            consensus_revenue = None
            if (
                revenue_frame is not None
                and not revenue_frame.empty
                and period_label in revenue_frame.index
            ):
                consensus_revenue = _number(revenue_frame.loc[period_label].get("avg"))
            low = _number(row.get("low"))
            high = _number(row.get("high"))
            analyst_count = row.get("numberOfAnalysts")
            observations.append(
                {
                    "fiscal_period": fiscal_period,
                    "consensus_eps": consensus_eps,
                    "consensus_revenue": consensus_revenue,
                    "analyst_count": int(analyst_count) if pd.notna(analyst_count) else None,
                    "estimate_dispersion": (
                        high - low if low is not None and high is not None else None
                    ),
                }
            )
        return observations

    def get_news(self, ticker: str, since: date | None = None) -> list[dict[str, Any]]:
        """Recent news items for `ticker`, best-effort normalized to
        `{title, url, publisher, summary, published_at, raw}`.

        SCHEMA CAVEAT (explicit, not glossed over): yfinance's news endpoint
        has changed JSON shape across library versions (an older flat
        `{title, link, publisher, providerPublishTime}` shape, and a newer
        nested `{"content": {...}}` shape), and this environment has no
        network access to confirm which shape yfinance 1.7.0 actually
        returns live. `_normalize_news_item` below tries both known shapes
        and returns None for anything that matches neither -- such an item
        is dropped by the caller (`NewsService.refresh`), never guessed
        into a fabricated record. If the live shape turns out to be a
        third, unrecognized form, every item is safely dropped (an honest
        `coverage == 0` outcome) rather than silently returning wrong data.

        `since` is accepted for interface compliance
        (`ResearchNewsProvider.get_news`) but yfinance's `Ticker.news`
        exposes no date-range parameter -- it only returns whatever Yahoo
        currently has cached for this ticker (recent items, not a
        historical archive). Incrementality is therefore handled entirely
        on the storage side (`NewsService.refresh`'s content-hash
        deduplication), not by this call. This is also why this feed can
        never retroactively backfill news that existed before AlphaLab
        started refreshing a given ticker -- see the module docstring in
        `alpha_lab.news.service`.
        """
        raw_items = call_with_classification(
            lambda: self._ticker(ticker).get_news(count=20),
            provider=self.provider_name,
        )
        normalized: list[dict[str, Any]] = []
        for item in raw_items or []:
            record = _normalize_news_item(item)
            if record is not None:
                normalized.append(record)
        return normalized

    def get_analyst_rating_changes(self, ticker: str) -> list[dict[str, Any]]:
        """Full analyst rating-change history (upgrades/downgrades/
        initiations/reiterations) for `ticker`, each a discrete graded
        event with the source's own historical timestamp.

        yfinance's ``upgradeDowngradeHistory`` module returns the ENTIRE
        history in one call, not only events since the last refresh --
        confirmed live (Phase 2I investigation): MA/AAL each returned
        several hundred rows spanning years. Callers persist via
        ``alpha_lab.ingestion.analyst_events.snapshot_analyst_rating_changes``,
        which content-hash-dedupes, so repeatedly re-fetching this same
        full history is idempotent rather than duplicating rows -- there is
        no need to track "since last refresh" here.

        Genuinely no analyst coverage for this instrument type (confirmed
        live for ETFs: yfinance returns an empty frame via a handled 404,
        not an exception -- the same pattern as ``get_estimates``) returns
        an empty list, never fabricated rows.

        A price target of exactly 0 (yfinance's placeholder when an
        initiation has no "prior" target to report) is normalized to None
        via ``_number(..., positive=True)`` -- a stock's price target is
        never genuinely $0, so 0 here means "not applicable", not "zero".
        """
        frame = call_with_classification(
            lambda: self._ticker(ticker).get_upgrades_downgrades(),
            provider=self.provider_name,
        )
        if frame is None or frame.empty:
            return []
        events: list[dict[str, Any]] = []
        for grade_date, row in frame.iterrows():
            grade_date_value = _parse_timestamp_like(grade_date)
            if grade_date_value is None:
                continue
            events.append(
                {
                    "grade_date": grade_date_value,
                    "firm": _text(row.get("Firm")),
                    "to_grade": _text(row.get("ToGrade")),
                    "from_grade": _text(row.get("FromGrade")),
                    "action": _text(row.get("Action")),
                    "price_target_action": _text(row.get("priceTargetAction")),
                    "current_price_target": _number(
                        row.get("currentPriceTarget"), positive=True
                    ),
                    "prior_price_target": _number(
                        row.get("priorPriceTarget"), positive=True
                    ),
                }
            )
        return events

    def get_estimate_revision_trend(
        self, ticker: str, observation_date: date
    ) -> list[dict[str, Any]]:
        """The source's own already-computed EPS-estimate trend (current,
        7/30/60/90 days ago) and analyst up/down revision counts, for
        `ticker` as observed on `observation_date` -- genuine revision
        evidence available from a single live call, unlike
        ``alpha_lab.ratings.estimates.calculate_revision_factors``, which
        can only derive a revision signal after multiple ``Estimate``
        observations accumulate over real elapsed time.

        Only "0y"/"+1y" fiscal periods are captured, using the exact same
        precise annual-only anchors as ``get_estimates`` (the quarterly
        labels this source also reports are dropped here for the identical
        date-precision reason documented on ``get_estimates``) -- so a
        period recorded here always matches the corresponding ``Estimate``
        row's ``fiscal_period`` exactly.

        Genuinely no coverage (confirmed live for ETFs) returns an empty
        list. This data is never wired into
        ``alpha_lab.screener.service``'s ``analyst_revisions`` scoring
        category or any other scoring input -- see
        ``alpha_lab.database.models.EstimateRevisionTrend``'s docstring.
        """
        ticker_obj = self._ticker(ticker)
        trend_frame = call_with_classification(
            lambda: ticker_obj.get_eps_trend(), provider=self.provider_name
        )
        if trend_frame is None or trend_frame.empty:
            return []
        revisions_frame = call_with_classification(
            lambda: ticker_obj.get_eps_revisions(), provider=self.provider_name
        )
        info = call_with_classification(
            lambda: ticker_obj.get_info(), provider=self.provider_name
        )
        anchors = _fiscal_anchors(info)

        observations: list[dict[str, Any]] = []
        for period_label, row in trend_frame.iterrows():
            fiscal_period = _fiscal_period_for(str(period_label), anchors)
            if fiscal_period is None:
                continue
            eps_trend_current = _number(row.get("current"))
            if eps_trend_current is None:
                continue
            revisions_row = (
                revisions_frame.loc[period_label]
                if revisions_frame is not None
                and not revisions_frame.empty
                and period_label in revisions_frame.index
                else None
            )
            observations.append(
                {
                    "fiscal_period": fiscal_period,
                    "eps_trend_current": eps_trend_current,
                    "eps_trend_7d_ago": _number(row.get("7daysAgo")),
                    "eps_trend_30d_ago": _number(row.get("30daysAgo")),
                    "eps_trend_60d_ago": _number(row.get("60daysAgo")),
                    "eps_trend_90d_ago": _number(row.get("90daysAgo")),
                    "revisions_up_last_7d": _int_or_none(
                        revisions_row.get("upLast7days") if revisions_row is not None else None
                    ),
                    "revisions_up_last_30d": _int_or_none(
                        revisions_row.get("upLast30days") if revisions_row is not None else None
                    ),
                    "revisions_down_last_7d": _int_or_none(
                        revisions_row.get("downLast7Days") if revisions_row is not None else None
                    ),
                    "revisions_down_last_30d": _int_or_none(
                        revisions_row.get("downLast30days") if revisions_row is not None else None
                    ),
                    "currency": _text(row.get("currency")),
                }
            )
        return observations


def _yahoo_symbol(ticker: str) -> str:
    """Translate AlphaLab's canonical ticker (as stored in `Security.ticker`,
    following the universe listing's own share-class notation) into the
    symbol Yahoo Finance actually recognizes. Only the outbound request
    to Yahoo is affected -- every value this provider returns still uses
    the original `ticker` (see e.g. `get_company_info`'s `"ticker":
    ticker.upper()`), so callers never see anything but the canonical
    symbol.

    Two real notations were confirmed live to 404 permanently against
    Yahoo, every single refresh, until translated -- see
    `alpha_lab.providers.ticker_notation` (shared with SEC EDGAR, which
    turned out to need the identical translation)."""
    return to_hyphenated_symbol(ticker)


def _normalize_news_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort extraction across the two known yfinance news shapes.
    Returns None (never a guess) if neither shape's required fields are
    present -- see `YFinanceProvider.get_news`'s schema caveat."""
    if not isinstance(item, dict):
        return None

    content = item.get("content") if isinstance(item.get("content"), dict) else None

    if content is not None:
        title = content.get("title")
        publisher = (content.get("provider") or {}).get("displayName") if isinstance(content.get("provider"), dict) else None
        url = None
        for url_field in ("canonicalUrl", "clickThroughUrl"):
            candidate = content.get(url_field)
            if isinstance(candidate, dict) and candidate.get("url"):
                url = candidate["url"]
                break
        summary = content.get("summary") or content.get("description")
        published_raw = content.get("pubDate") or content.get("displayTime")
        published_at = _parse_timestamp(published_raw)
    else:
        title = item.get("title")
        publisher = item.get("publisher")
        url = item.get("link")
        summary = item.get("summary")
        published_at = _parse_timestamp(item.get("providerPublishTime"))

    if not title or not url or published_at is None:
        return None

    return {
        "title": title,
        "url": url,
        "publisher": publisher,
        "summary": summary,
        "published_at": published_at,
        "raw": item,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    """Accepts a unix timestamp (legacy shape) or an ISO-8601 string
    (newer shape). Returns None -- never a guessed date -- for anything
    else."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def _current_recommendation_counts(frame: pd.DataFrame) -> dict[str, int | None]:
    """The "0m" (current month) row of yfinance's recommendationTrend, or an
    empty dict if that row is missing -- never a guessed/zeroed fallback."""
    if frame is None or frame.empty or "period" not in frame:
        return {}
    current = frame[frame["period"] == "0m"]
    if current.empty:
        return {}
    row = current.iloc[0]
    result: dict[str, int | None] = {}
    for column in ("strongBuy", "buy", "hold", "sell", "strongSell"):
        if column not in row or pd.isna(row[column]):
            result[column] = None
        else:
            result[column] = int(row[column])
    return result


def _number(value: Any, *, positive: bool = False) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _text(value: Any) -> str | None:
    """A pandas cell as a plain string, or None -- never the literal string
    'nan' that `str(float('nan'))` would otherwise produce."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _fund_table_value(frame: pd.DataFrame | None, row_label: str, column_index: int) -> float | None:
    """One cell of a `FundsData.fund_operations`/`.equity_holdings`-shaped
    DataFrame (index = attribute name, columns = [the fund's own value,
    "Category Average"]) -- accessed positionally by `column_index` rather
    than by the fund's own ticker as a column name, since a share-class
    suffix or casing difference could otherwise silently miss the column.
    None when the frame is absent, the row doesn't exist, the column
    doesn't exist, or the cell is `pd.NA` -- never a fabricated 0."""
    if frame is None or frame.empty or row_label not in frame.index:
        return None
    row = frame.loc[row_label]
    if column_index >= len(row):
        return None
    return _number(row.iloc[column_index])


def _top_holdings_rows(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    """`FundsData.top_holdings` (index = holding's own ticker symbol,
    columns = ["Name", "Holding Percent"]) as a plain list of dicts, in
    the same order yfinance returned them -- already Yahoo's own top-N,
    never re-sorted or truncated here."""
    if frame is None or frame.empty:
        return []
    rows = []
    for symbol, row in frame.iterrows():
        rows.append(
            {
                "symbol": _text(symbol),
                "name": _text(row.get("Name")),
                "weight": _number(row.get("Holding Percent"), positive=True),
            }
        )
    return rows


def _int_or_none(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp_like(value: Any) -> datetime | None:
    """A pandas Timestamp/datetime index value (yfinance's `GradeDate`
    index) as a naive UTC-ish datetime, or None if it isn't a real
    timestamp -- never a guessed date."""
    if value is None or pd.isna(value):
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.to_pydatetime().replace(tzinfo=None)


def _epoch_to_date(value: Any) -> date | None:
    """A yfinance ``get_info()`` unix-timestamp field (e.g. ``mostRecentQuarter``),
    or None -- never a guessed date -- if absent or unparseable."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _fiscal_anchors(info: dict[str, Any]) -> dict[str, date | None]:
    """Real, provider-given anchor date this ticker's year-relative
    consensus-estimate periods are derived from -- never guessed."""
    return {"next_fiscal_year_end": _epoch_to_date(info.get("nextFiscalYearEnd"))}


def _add_months(value: date, months: int) -> date:
    total_month_index = value.month - 1 + months
    year = value.year + total_month_index // 12
    month = total_month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _fiscal_period_for(period_label: str, anchors: dict[str, date | None]) -> date | None:
    """Derive the fiscal-period-end date for one of yfinance's relative
    consensus-estimate period labels. Only "0y" (this company's own
    ``next_fiscal_year_end``, used directly) and "+1y" (exactly 12 months
    after it) are supported -- both precise. "0q"/"+1q" are deliberately
    unsupported (return None, never a fabricated date): see
    ``YFinanceProvider.get_estimates``'s docstring for why their end date
    cannot be derived precisely from what ``get_info()`` reports.
    """
    next_fiscal_year_end = anchors.get("next_fiscal_year_end")
    if next_fiscal_year_end is None:
        return None
    if period_label == "0y":
        return next_fiscal_year_end
    if period_label == "+1y":
        return _add_months(next_fiscal_year_end, 12)
    return None
