"""Fund-specific research evidence (PR #30): holdings, sector/asset-class
allocation, fund operations (expense ratio, AUM/total net assets), and
equity-holdings valuation averages for ETFs and other funds.

Distinct from the eight equity fundamental-scoring categories -- six of
which `alpha_lab.research.security_type` already classifies
`NOT_APPLICABLE` for an ETF (business_quality, earnings_growth,
financial_strength, valuation, analyst_revisions, shareholder_return: all
structurally dependent on a company's own income statement/balance
sheet/EPS estimates/buybacks, which a fund does not have). This module is
the evidence that genuinely does exist for a fund instead, per the project
roadmap's PR #30 spec ("make ETF research genuinely useful"). Never
touches `StockResearch.overall_score`/`categories`, and never computes a
second composite score of its own.

Pure and deterministic: takes an already-fetched raw dict (from
`alpha_lab.providers.yfinance_provider.YFinanceProvider.get_fund_data`),
never calls a provider or touches the database.

Deliberately scoped to exactly the fields confirmed live to be reliably
populated for the installed yfinance version (see `get_fund_data`'s own
docstring for the full investigation): fund identity/category, expense
ratio/holdings turnover/total net assets, asset-class mix, sector weights,
top holdings, and four equity-holdings valuation averages (P/E, P/B, P/S,
P/CF). `bond_holdings`/`bond_ratings` and `info`-dict distribution/
performance fields are excluded there, not here -- this module simply has
no input for them because the provider layer never fetches them.
"""

from datetime import date

from pydantic import BaseModel, Field

FUND_EVIDENCE_METHODOLOGY_VERSION = "fund-evidence-v1"

# The five evidence domains a FundEvidence can independently carry --
# mirrors AnalystResearchSummary's domain-aware coverage pattern (each
# domain contributes equally to `coverage`; a domain that never populates
# for this fund counts as absent, never excluded from the denominator).
_DOMAIN_COUNT = 5


class TopHolding(BaseModel):
    symbol: str | None
    name: str | None
    weight: float | None = Field(None, ge=0, le=1)


class AssetAllocation(BaseModel):
    """Fractions of fund assets by broad instrument type. Individually
    optional -- a fund with a genuinely reported 0.0 position (e.g. no
    bonds) is a real zero, distinct from the whole domain being absent
    (all six fields None) when yfinance returned no asset-class data at
    all for this fund."""

    cash: float | None = Field(None, ge=0, le=1)
    stock: float | None = Field(None, ge=0, le=1)
    bond: float | None = Field(None, ge=0, le=1)
    preferred: float | None = Field(None, ge=0, le=1)
    convertible: float | None = Field(None, ge=0, le=1)
    other: float | None = Field(None, ge=0, le=1)

    def is_present(self) -> bool:
        return any(
            value is not None
            for value in (self.cash, self.stock, self.bond, self.preferred, self.convertible, self.other)
        )


class FundOperations(BaseModel):
    """`total_net_assets` (AUM) is in whatever units the provider reports
    (yfinance's `funds_data.fund_operations` -- observed live in millions
    of the fund's trading currency); never rescaled or assumed here."""

    expense_ratio: float | None = Field(None, ge=0)
    category_avg_expense_ratio: float | None = Field(None, ge=0)
    holdings_turnover: float | None = Field(None, ge=0)
    total_net_assets: float | None = Field(None, ge=0)

    def is_present(self) -> bool:
        return any(
            value is not None
            for value in (
                self.expense_ratio,
                self.holdings_turnover,
                self.total_net_assets,
            )
        )


class EquityHoldingsCharacteristics(BaseModel):
    """Fund-level average valuation ratios across its equity holdings.
    Deliberately excludes Median Market Cap/3 Year Earnings Growth --
    confirmed unpopulated (`<NA>`) for every fund checked; see
    `get_fund_data`'s docstring."""

    price_earnings: float | None = None
    price_book: float | None = None
    price_sales: float | None = None
    price_cashflow: float | None = None

    def is_present(self) -> bool:
        return any(
            value is not None
            for value in (
                self.price_earnings,
                self.price_book,
                self.price_sales,
                self.price_cashflow,
            )
        )


class FundEvidence(BaseModel):
    ticker: str
    category_name: str | None
    fund_family: str | None
    legal_type: str | None
    description: str | None
    asset_allocation: AssetAllocation
    # Sector name -> weight; only sectors yfinance actually reported, in
    # whatever order it returned them. An empty dict means the whole
    # domain was absent, never a fabricated all-zero breakdown.
    sector_weightings: dict[str, float]
    operations: FundOperations
    equity_holdings: EquityHoldingsCharacteristics
    # Yahoo's own top-N holdings, in the order it returned them -- never
    # re-sorted, re-ranked, or truncated here.
    top_holdings: list[TopHolding]
    # Sum of top_holdings' weights -- the roadmap's "concentration"
    # evidence. None (never a fabricated 0.0) when top_holdings is empty.
    top_holdings_concentration: float | None = Field(None, ge=0, le=1)
    # 0.0-1.0 across the five domains actually present (asset allocation,
    # sector weightings, operations, equity holdings, top holdings) --
    # mirrors AnalystResearchSummary/AIEvidenceCoverage's domain-aware
    # pattern (absent counts as 0, never excluded from the denominator).
    coverage: float = Field(ge=0, le=1)
    as_of: date
    methodology_version: str = FUND_EVIDENCE_METHODOLOGY_VERSION
    source: str
    # Namespaced evidence IDs (fund:asset_allocation, fund:sector_
    # weightings, fund:operations, fund:equity_holdings, fund:top_holdings)
    # for exactly the domains that are present -- traces this summary back
    # to what genuinely contributed to it.
    evidence_ids: list[str]


def build_fund_evidence(raw: dict) -> FundEvidence | None:
    """Pure construction from `YFinanceProvider.get_fund_data`'s raw dict.
    Returns `None` (never an empty-but-present object) when every domain is
    absent -- matching `AnalystResearchSummary`'s "None means not computed"
    convention. `raw` being `None` (the provider's own "not a fund" signal)
    must be checked by the caller before calling this at all; this function
    assumes `raw` is a genuine fund-data dict."""
    asset_allocation = AssetAllocation(
        cash=raw.get("cash_position"),
        stock=raw.get("stock_position"),
        bond=raw.get("bond_position"),
        preferred=raw.get("preferred_position"),
        convertible=raw.get("convertible_position"),
        other=raw.get("other_position"),
    )
    sector_weightings = {
        sector: weight
        for sector, weight in (raw.get("sector_weightings") or {}).items()
        if weight is not None
    }
    operations = FundOperations(
        expense_ratio=raw.get("expense_ratio"),
        category_avg_expense_ratio=raw.get("category_avg_expense_ratio"),
        holdings_turnover=raw.get("holdings_turnover"),
        total_net_assets=raw.get("total_net_assets"),
    )
    equity_holdings = EquityHoldingsCharacteristics(
        price_earnings=raw.get("price_earnings"),
        price_book=raw.get("price_book"),
        price_sales=raw.get("price_sales"),
        price_cashflow=raw.get("price_cashflow"),
    )
    top_holdings = [
        TopHolding(symbol=row.get("symbol"), name=row.get("name"), weight=row.get("weight"))
        for row in raw.get("top_holdings") or []
    ]

    domains_present = [
        asset_allocation.is_present(),
        bool(sector_weightings),
        operations.is_present(),
        equity_holdings.is_present(),
        bool(top_holdings),
    ]
    if not any(domains_present):
        return None

    # None (never a silently-partial sum) unless every top holding has a
    # genuine weight -- summing only the ones that happen to be present
    # would understate concentration without any signal that it did, and
    # the surfaced evidence text ("Top N holdings concentration = X%")
    # implies a sum over all N.
    concentration = (
        sum(holding.weight for holding in top_holdings)
        if top_holdings and all(holding.weight is not None for holding in top_holdings)
        else None
    )

    evidence_ids = []
    if asset_allocation.is_present():
        evidence_ids.append("fund:asset_allocation")
    if sector_weightings:
        evidence_ids.append("fund:sector_weightings")
    if operations.is_present():
        evidence_ids.append("fund:operations")
    if equity_holdings.is_present():
        evidence_ids.append("fund:equity_holdings")
    if top_holdings:
        evidence_ids.append("fund:top_holdings")

    return FundEvidence(
        ticker=raw["ticker"],
        category_name=raw.get("category_name"),
        fund_family=raw.get("fund_family"),
        legal_type=raw.get("legal_type"),
        description=raw.get("description"),
        asset_allocation=asset_allocation,
        sector_weightings=sector_weightings,
        operations=operations,
        equity_holdings=equity_holdings,
        top_holdings=top_holdings,
        top_holdings_concentration=concentration,
        coverage=sum(domains_present) / _DOMAIN_COUNT,
        as_of=raw["as_of"],
        source=raw["source"],
        evidence_ids=evidence_ids,
    )
