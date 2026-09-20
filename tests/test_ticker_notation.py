"""Tests for the shared canonical-ticker -> external-provider-symbol
translation (alpha_lab.providers.ticker_notation), used by both Yahoo
Finance and SEC EDGAR -- both were confirmed live to use the identical
hyphen-based notation for AlphaLab's dot-separated share classes and
dollar-sign preferred-share suffixes."""

import pytest

from alpha_lab.providers.ticker_notation import to_hyphenated_symbol


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("BRK.B", "BRK-B"),
        ("AGM.A", "AGM-A"),
        ("AHL$D", "AHL-PD"),
        ("EPR$E", "EPR-PE"),
        ("NVDA", "NVDA"),
        ("AAPL", "AAPL"),
    ],
)
def test_to_hyphenated_symbol(ticker, expected):
    assert to_hyphenated_symbol(ticker) == expected
