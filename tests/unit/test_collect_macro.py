"""tests/unit/test_collect_macro.py(30.8.4)。"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from autoscreener.batch.collect_macro import collect_macro
from autoscreener.collectors.fred_client import SeriesObservation
from autoscreener.config import FredConfig
from autoscreener.db.models import MacroSeries
from autoscreener.db.session import session_scope


def _cleanup(series_ids: list[str]) -> None:
    with session_scope() as session:
        session.query(MacroSeries).filter(MacroSeries.series_id.in_(series_ids)).delete(synchronize_session=False)


def test_disabled_without_api_key_returns_zero_counts():
    with patch("autoscreener.batch.collect_macro.get_settings") as mock_settings:
        mock_settings.return_value.fred_api_key = None
        counts = collect_macro(fred_config=FredConfig(enabled=True, series_ids=["DGS10"]))
    assert counts == {"series": 0, "observations_upserted": 0}


def test_disabled_by_config_returns_zero_counts():
    with patch("autoscreener.batch.collect_macro.get_settings") as mock_settings:
        mock_settings.return_value.fred_api_key = "test-key"
        counts = collect_macro(fred_config=FredConfig(enabled=False, series_ids=["DGS10"]))
    assert counts == {"series": 0, "observations_upserted": 0}


def test_collect_macro_upserts_without_duplicate_rows():
    series_id = "ZZTESTMACRO"
    _cleanup([series_id])
    try:
        with (
            patch("autoscreener.batch.collect_macro.get_settings") as mock_settings,
            patch("autoscreener.batch.collect_macro.FredClient") as mock_client_cls,
        ):
            mock_settings.return_value.fred_api_key = "test-key"
            mock_client_cls.return_value.fetch_series.return_value = [
                SeriesObservation(observation_date=datetime.date(2026, 8, 1), value=4.25),
                SeriesObservation(observation_date=datetime.date(2026, 8, 2), value=None),  # 欠測はスキップ
            ]
            counts_1 = collect_macro(fred_config=FredConfig(enabled=True, series_ids=[series_id]))
            # 同じ観測日を再取得しても重複行が入らないこと
            counts_2 = collect_macro(fred_config=FredConfig(enabled=True, series_ids=[series_id]))

        assert counts_1["series"] == 1
        assert counts_1["observations_upserted"] == 1
        assert counts_2["observations_upserted"] == 1  # upsertなので2回目も1件更新

        with session_scope() as session:
            rows = session.query(MacroSeries).filter_by(series_id=series_id).all()
            assert len(rows) == 1
            assert float(rows[0].value) == 4.25
    finally:
        _cleanup([series_id])


def test_one_series_failure_does_not_stop_others():
    with (
        patch("autoscreener.batch.collect_macro.get_settings") as mock_settings,
        patch("autoscreener.batch.collect_macro.FredClient") as mock_client_cls,
    ):
        mock_settings.return_value.fred_api_key = "test-key"

        def _side_effect(series_id, **kwargs):
            if series_id == "BAD":
                raise RuntimeError("boom")
            return [SeriesObservation(observation_date=datetime.date(2026, 8, 1), value=1.0)]

        mock_client_cls.return_value.fetch_series.side_effect = _side_effect
        try:
            counts = collect_macro(fred_config=FredConfig(enabled=True, series_ids=["BAD", "ZZTESTMACRO2"]))
            assert counts["series"] == 1  # BADは失敗、もう一方は成功
        finally:
            _cleanup(["ZZTESTMACRO2"])
