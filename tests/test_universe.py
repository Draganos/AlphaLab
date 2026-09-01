from alpha_lab.research import load_universe
from alpha_lab.providers import CSVSecurityUniverseProvider
from alpha_lab.search import ScreenCriteria, ScreenRecord, apply_screen
from alpha_lab.providers.nasdaq_universe import _likely_common_stock


def test_membership_columns_do_not_claim_unimplemented_historical_support(tmp_path):
    path = tmp_path / "members.csv"
    path.write_text("ticker,membership_start,membership_end\nA,2020-01-01,2021-01-01\n")
    universe = load_universe(path)
    assert "SURVIVORSHIP BIAS RISK" in universe.limitation
    assert "not applied" in universe.limitation


def test_csv_universe_blank_numeric_values_become_none_and_fail_numeric_filter(
    tmp_path,
):
    path = tmp_path / "universe.csv"
    path.write_text(
        "ticker,company_name,country,exchange,market_cap\n"
        "NULLCO,Null Company,US,NASDAQ,\n"
    )
    row = CSVSecurityUniverseProvider(path).get_securities()[0]
    assert row["market_cap"] is None
    record = ScreenRecord(
        ticker=row["ticker"],
        company_name=row["company_name"],
        ethical_status="PASS",
        market_cap=row["market_cap"],
    )
    assert apply_screen([record], ScreenCriteria(minimum_market_cap=1)) == []


def test_common_stock_filter_does_not_match_exclusion_inside_issuer_name():
    assert _likely_common_stock("BrightSpring Health Services, Inc. - Common Stock")
    assert not _likely_common_stock("Example Corp - Rights")


def test_csv_explicit_refresh_is_not_restricted_to_default_country(tmp_path):
    path = tmp_path / "global.csv"
    path.write_text("ticker,country,exchange\nUSCO,US,NASDAQ\nCAco,CA,TSX\n")
    rows = CSVSecurityUniverseProvider(path).refresh_metadata(["CACO"])
    assert rows[0]["country"] == "CA"
