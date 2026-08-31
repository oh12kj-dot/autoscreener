"""tests/unit/test_run_monitoring.py(30.7.6)。"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from autoscreener.batch.run_monitoring import run_monitoring
from autoscreener.db.models import Alert, Filing, RawSnapshot, Ticker
from autoscreener.db.session import session_scope

_SYMBOL = "ZZMON1"


def _cleanup():
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one_or_none()
        if ticker is not None:
            session.query(Alert).filter_by(ticker_id=ticker.id).delete()
            session.query(Filing).filter_by(ticker_id=ticker.id).delete()
            session.query(RawSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


@pytest.fixture
def seeded_ticker_with_low_cash_runway():
    _cleanup()
    with session_scope() as session:
        ticker = Ticker(symbol=_SYMBOL, market="US", cik="0000320193")
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=datetime.date(2026, 8, 1),
                source="test",
                payload={
                    "info": {"totalCash": 10.0, "currentPrice": 30.0},
                    "quarterly_income_stmt": {},
                    "quarterly_cash_flow": {
                        "Free Cash Flow": {
                            "2026-06-30": -10.0,
                            "2025-03-31": -10.0,
                            "2024-12-31": -10.0,
                            "2024-09-30": -10.0,
                        }
                    },
                },
                content_hash="h1",
                last_seen_date=datetime.date(2026, 8, 1),
                available_from=datetime.date(2026, 8, 1),
            )
        )
    yield _SYMBOL
    _cleanup()


def test_run_monitoring_creates_alert_for_triggered_metric(seeded_ticker_with_low_cash_runway):
    with patch("autoscreener.batch.run_monitoring._target_tickers") as mock_targets:
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one()
            mock_targets.return_value = [ticker]
        counts_1 = run_monitoring(as_of=datetime.date(2026, 8, 28))
        counts_2 = run_monitoring(as_of=datetime.date(2026, 8, 29))  # 翌日も再実行

    assert counts_1["new_alerts"] >= 1
    assert counts_2["already_open"] >= 1  # 2日連続で発生しても行は増えない

    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one()
        alerts = session.query(Alert).filter_by(ticker_id=ticker.id, code="cash_runway_low").all()
        assert len(alerts) == 1  # 未解消のまま2回走らせても1件のまま


def test_run_monitoring_no_tickers_returns_zero_counts():
    with patch("autoscreener.batch.run_monitoring._target_tickers", return_value=[]):
        counts = run_monitoring()
    assert counts == {"tickers": 0, "new_alerts": 0, "already_open": 0}


# --- J-8(docs/investment_decision_gap_2026-08-29.md):利食い閾値の到達アラート ---


class _FakeNote:
    def __init__(self, front_matter: dict) -> None:
        self.front_matter = front_matter


def test_run_monitoring_fires_trim_threshold_alert_once(seeded_ticker_with_low_cash_runway):
    from autoscreener.config import Position

    position = Position(
        ticker=_SYMBOL,
        opened_on=datetime.date(2025, 1, 1),
        shares=10,
        cost_basis_usd=10.0,  # 現在値 30 → 達成倍率 3.0
    )
    note = _FakeNote(
        {"exit_plan": {"trim_rule": [{"at_moic": 3.0, "action": "1/3 売却"}, {"at_moic": 6.0}]}}
    )

    with (
        patch("autoscreener.batch.run_monitoring._target_tickers") as mock_targets,
        patch("autoscreener.batch.run_monitoring.load_positions_config") as mock_positions,
        patch("autoscreener.batch.run_monitoring.load_note", return_value=note),
    ):
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one()
            mock_targets.return_value = [ticker]
        mock_positions.return_value = type("C", (), {"positions": [position]})()

        run_monitoring(as_of=datetime.date(2026, 8, 28))
        run_monitoring(as_of=datetime.date(2026, 8, 29))  # 翌日も再実行

    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one()
        trim_alerts = (
            session.query(Alert)
            .filter(Alert.ticker_id == ticker.id, Alert.code == "trim_threshold_3")
            .all()
        )
        assert len(trim_alerts) == 1  # 同じ閾値で2回は出ない
        assert trim_alerts[0].severity == "info"
        # 6.0 の段はまだ未到達
        assert (
            session.query(Alert)
            .filter(Alert.ticker_id == ticker.id, Alert.code == "trim_threshold_6")
            .count()
            == 0
        )
