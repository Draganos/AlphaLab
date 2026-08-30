"""Official SEC Companyfacts client and conservative XBRL parser."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import hashlib
import json
import math
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SEC_BASE = "https://data.sec.gov"
SUPPORTED_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}

# Metric -> ordered (concept, required unit). Precedence is explicit and issuer
# extensions are deliberately unsupported because their semantics are ambiguous.
CONCEPT_MAPPING: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
        ("Revenues", "USD"),
        ("SalesRevenueNet", "USD"),
    ),
    "gross_profit": (("GrossProfit", "USD"),),
    "ebit": (("OperatingIncomeLoss", "USD"),),
    "net_income": (("NetIncomeLoss", "USD"),),
    "eps": (("EarningsPerShareDiluted", "USD/shares"),),
    "cash": (
        ("CashAndCashEquivalentsAtCarryingValue", "USD"),
        ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "USD"),
    ),
    "total_debt": (
        ("LongTermDebtAndFinanceLeaseObligations", "USD"),
    ),
    "total_equity": (("StockholdersEquity", "USD"),),
    "total_assets": (("Assets", "USD"),),
    "current_assets": (("AssetsCurrent", "USD"),),
    "current_liabilities": (("LiabilitiesCurrent", "USD"),),
    "dividends_paid": (("PaymentsOfDividends", "USD"),),
    "share_repurchases": (("PaymentsForRepurchaseOfCommonStock", "USD"),),
}

CONCEPT_TO_METRIC = {
    concept: (metric, rank, unit)
    for metric, concepts in CONCEPT_MAPPING.items()
    for rank, (concept, unit) in enumerate(concepts)
}


@dataclass(frozen=True)
class SECFact:
    ticker: str
    cik: str
    taxonomy: str
    concept: str
    metric: str
    unit: str
    value: float
    period_start: date | None
    period_end: date
    filed_date: date
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    accession: str
    frame: str | None
    source_url: str


class SECClient:
    """Small cached SEC JSON client with honest identity, pacing, and bounded retries."""

    def __init__(
        self,
        user_agent: str,
        cache_dir: str | Path = "data/cache/sec",
        *,
        minimum_interval: float = 0.12,
        retries: int = 2,
    ):
        if not user_agent.strip() or "@" not in user_agent:
            raise ValueError("SEC User-Agent must identify an application and contact email")
        self.user_agent = user_agent
        self.cache_dir = Path(cache_dir)
        self.minimum_interval = minimum_interval
        self.retries = retries
        self._last_request = 0.0

    def get_json(self, path: str, *, refresh: bool = False) -> dict[str, Any]:
        url = path if path.startswith("https://") else f"{SEC_BASE}{path}"
        cache = self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"
        if cache.exists() and not refresh:
            return json.loads(cache.read_text(encoding="utf-8"))
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                wait = self.minimum_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                request = Request(url, headers={"User-Agent": self.user_agent})
                with urlopen(request, timeout=30) as response:  # noqa: S310
                    payload = json.loads(response.read())
                self._last_request = time.monotonic()
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(payload), encoding="utf-8")
                return payload
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                error = exc
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"SEC request failed without modifying stored data: {url}") from error


class SECCompanyFactsProvider:
    provider_name = "SECCompanyFactsProvider"

    def __init__(self, client: SECClient):
        self.client = client

    def company_tickers(self) -> dict[str, str]:
        payload = self.client.get_json("/files/company_tickers.json")
        return {
            str(row["ticker"]).upper(): str(row["cik_str"]).zfill(10)
            for row in payload.values()
        }

    def get_facts(self, ticker: str, cik: str, *, as_of: date | None = None) -> list[SECFact]:
        path = f"/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
        return parse_companyfacts(
            self.client.get_json(path), ticker.upper(), cik.zfill(10),
            source_url=f"{SEC_BASE}{path}", as_of=as_of,
        )


def parse_companyfacts(
    payload: dict[str, Any], ticker: str, cik: str, *, source_url: str,
    as_of: date | None = None,
) -> list[SECFact]:
    """Parse only mapped US-GAAP facts with compatible units and knowledge dates."""
    parsed: list[SECFact] = []
    facts = payload.get("facts", {}).get("us-gaap", {})
    for concept, definition in facts.items():
        mapping = CONCEPT_TO_METRIC.get(concept)
        if mapping is None:
            continue
        metric, _, expected_unit = mapping
        for unit, rows in definition.get("units", {}).items():
            if unit != expected_unit:
                continue
            for row in rows:
                try:
                    filed = date.fromisoformat(row["filed"])
                    value = float(row["val"])
                    form = str(row["form"])
                    end = date.fromisoformat(row["end"])
                    accession = str(row["accn"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    form not in SUPPORTED_FORMS
                    or (as_of is not None and filed > as_of)
                    or not math.isfinite(value)
                ):
                    continue
                start = row.get("start")
                parsed.append(SECFact(
                    ticker=ticker, cik=cik, taxonomy="us-gaap", concept=concept,
                    metric=metric, unit=unit, value=value,
                    period_start=date.fromisoformat(start) if start else None,
                    period_end=end, filed_date=filed, form=form,
                    fiscal_year=int(row["fy"]) if row.get("fy") is not None else None,
                    fiscal_period=row.get("fp"), accession=accession,
                    frame=row.get("frame"), source_url=source_url,
                ))
    return _drop_conflicting_duplicates(parsed)


def _drop_conflicting_duplicates(facts: list[SECFact]) -> list[SECFact]:
    grouped: dict[tuple[Any, ...], list[SECFact]] = {}
    for fact in facts:
        key = (fact.accession, fact.concept, fact.unit, fact.period_start, fact.period_end)
        grouped.setdefault(key, []).append(fact)
    return [rows[0] for rows in grouped.values() if len({row.value for row in rows}) == 1]


def select_filing_metrics(facts: list[SECFact]) -> list[dict[str, Any]]:
    """Build filing-version snapshots using concept precedence inside each accession."""
    filings: dict[tuple[str, date, date, str, str | None], list[SECFact]] = {}
    for fact in facts:
        key = (fact.accession, fact.period_end, fact.filed_date, fact.form, fact.fiscal_period)
        filings.setdefault(key, []).append(fact)
    selected: list[dict[str, Any]] = []
    for (accession, period, filed, form, fiscal_period), rows in sorted(filings.items()):
        values: dict[str, float] = {}
        provenance: dict[str, dict[str, Any]] = {}
        for metric, concepts in CONCEPT_MAPPING.items():
            for concept, unit in concepts:
                matches = [row for row in rows if row.concept == concept and row.unit == unit]
                fact = _select_period_match(matches, form)
                if fact is not None:
                    values[metric] = fact.value
                    provenance[metric] = {
                        "concept": fact.concept, "unit": fact.unit,
                        "accession": fact.accession, "form": fact.form,
                        "filed_date": fact.filed_date.isoformat(),
                    }
                    break
        if values:
            selected.append({
                "period": period, "publication_date": filed, "accession": accession,
                "form": form, "fiscal_period": fiscal_period, "values": values,
                "provenance": provenance, "source": rows[0].source_url,
            })
    return selected


def _select_period_match(matches: list[SECFact], form: str) -> SECFact | None:
    if not matches:
        return None
    instant = [fact for fact in matches if fact.period_start is None]
    if instant:
        return instant[0] if len(instant) == 1 else None
    durations = [
        (fact.period_end - fact.period_start).days
        for fact in matches
        if fact.period_start is not None
    ]
    if form.startswith("10-K"):
        eligible = [
            fact for fact, days in zip(matches, durations, strict=True) if 300 <= days <= 380
        ]
    else:
        eligible = [
            fact for fact, days in zip(matches, durations, strict=True) if 60 <= days <= 120
        ]
    return eligible[0] if len(eligible) == 1 else None
