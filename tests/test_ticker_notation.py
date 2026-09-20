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


def test_to_hyphenated_symbol_leaves_a_non_share_class_dot_unchanged():
    """Regression test for a real live bug: a blanket ticker.replace(".",
    "-") corrupted "DX-Y.NYB" (AlphaLab's macro-proxy ticker for the US
    Dollar Index) into "DX-Y-NYB", which 404s against real Yahoo Finance
    -- confirmed live, "DX-Y.NYB" (unmodified) is Yahoo's actual correct
    symbol. Its dot is not a share-class separator at all, unlike "BRK.B"
    or "AGM.A"'s single-letter suffixes."""
    assert to_hyphenated_symbol("DX-Y.NYB") == "DX-Y.NYB"
