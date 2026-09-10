"""Optional US data provider. Missing fields remain null.

External call failures (rate limiting, network/DNS unavailability, an
unclassified provider error) are never treated as data -- they raise
``ProviderError`` for the caller to handle, instead of surfacing raw
yfinance/curl_cffi/requests internals or being silently swallowed into a
zero, an empty frame, or a fabricated value.
"""

from datetime import UTC, date, datetime
from typing import Any
import math
import pandas as pd

from alpha_lab.providers.base import MarketDataProvider
from alpha_lab.providers.errors import call_with_classification
from alpha_lab.providers.interfaces import ResearchNewsProvider


class YFinanceProvider(MarketDataProvider, ResearchNewsProvider):
    def _ticker(self, symbol: str):
        import yfinance as yf

        return yf.Ticker(symbol)

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
