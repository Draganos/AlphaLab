from alpha_lab.research import load_universe


def test_membership_columns_do_not_claim_unimplemented_historical_support(tmp_path):
    path = tmp_path / "members.csv"
    path.write_text("ticker,membership_start,membership_end\nA,2020-01-01,2021-01-01\n")
    universe = load_universe(path)
    assert "SURVIVORSHIP BIAS RISK" in universe.limitation
    assert "not applied" in universe.limitation
