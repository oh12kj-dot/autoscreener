"""tests/unit/test_tradability.py(30.2.4)。"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.screening.tradability import (
    NOT_LISTED,
    TRADABLE,
    UNKNOWN,
    evaluate_tradability,
    load_broker_coverage,
)
from autoscreener.symbols import symbol_variants


def _write(tmp_path, name, lines):
    path = tmp_path / f"{name}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_no_directory_returns_empty_coverage(tmp_path):
    coverage = load_broker_coverage(tmp_path / "does_not_exist")
    assert coverage == []


def test_empty_coverage_is_always_unknown():
    result = evaluate_tradability("AAPL", [])
    assert result.status == UNKNOWN
    assert result.brokers == []


def test_symbol_in_one_list_is_tradable(tmp_path):
    _write(tmp_path, "sbi", ["# comment", "", "AAPL", "ABCD"])
    coverage = load_broker_coverage(tmp_path)
    result = evaluate_tradability("AAPL", coverage)
    assert result.status == TRADABLE
    assert result.brokers == ["sbi"]


def test_symbol_not_in_any_list_is_not_listed(tmp_path):
    _write(tmp_path, "sbi", ["AAPL"])
    coverage = load_broker_coverage(tmp_path)
    result = evaluate_tradability("ZZZZ", coverage)
    assert result.status == NOT_LISTED


def test_dot_dash_variants_are_reconciled(tmp_path):
    _write(tmp_path, "sbi", ["BRK.B"])
    coverage = load_broker_coverage(tmp_path)
    result = evaluate_tradability("BRK-B", coverage)
    assert result.status == TRADABLE


def test_multiple_brokers_listed_sorted(tmp_path):
    _write(tmp_path, "rakuten", ["AAPL"])
    _write(tmp_path, "sbi", ["AAPL"])
    coverage = load_broker_coverage(tmp_path)
    result = evaluate_tradability("aapl", coverage)  # 小文字入力も正規化される
    assert result.status == TRADABLE
    assert result.brokers == ["rakuten", "sbi"]


def test_symbol_variants_includes_self():
    assert "AAPL" in symbol_variants("aapl")


def test_symbol_variants_handles_no_separator():
    assert symbol_variants("AAPL") == frozenset({"AAPL"})
