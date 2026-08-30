"""tests/unit/test_symbols.py"""

from __future__ import annotations

from autoscreener.symbols import normalize_symbol, symbol_variants


def test_normalize_strips_and_uppercases():
    assert normalize_symbol("  aapl ") == "AAPL"


def test_variants_dot_to_dash():
    assert symbol_variants("BRK.B") == frozenset({"BRK.B", "BRK-B"})


def test_variants_dash_to_dot():
    assert symbol_variants("BRK-B") == frozenset({"BRK-B", "BRK.B"})


def test_variants_no_separator():
    assert symbol_variants("AAPL") == frozenset({"AAPL"})
