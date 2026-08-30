"""tests/unit/test_positions_config.py(30.7.6)。"""

from __future__ import annotations

import datetime

import pytest
from pydantic import ValidationError

from autoscreener.config import Position, PositionsConfig, load_positions_config


def test_missing_file_returns_empty_positions(tmp_path):
    result = load_positions_config(tmp_path / "does_not_exist.yaml")
    assert result.positions == []


def test_valid_positions_file_parses(tmp_path):
    path = tmp_path / "positions.yaml"
    path.write_text(
        """
positions:
  - ticker: ABCD
    opened_on: 2026-08-20
    shares: 120
    cost_basis_usd: 14.32
    binary_event: false
    closed_on: null
""",
        encoding="utf-8",
    )
    result = load_positions_config(path)
    assert len(result.positions) == 1
    assert result.positions[0].ticker == "ABCD"
    assert result.positions[0].closed_on is None


def test_closed_position_is_parseable():
    position = Position(
        ticker="ABCD",
        opened_on=datetime.date(2026, 1, 1),
        shares=10,
        cost_basis_usd=5.0,
        closed_on=datetime.date(2026, 6, 1),
    )
    assert position.closed_on == datetime.date(2026, 6, 1)


def test_negative_shares_rejected():
    with pytest.raises(ValidationError):
        Position(ticker="ABCD", opened_on=datetime.date(2026, 1, 1), shares=-1, cost_basis_usd=5.0)
