import datetime
from contextlib import contextmanager
from unittest.mock import ANY, patch

import pytest

from autoscreener.batch.daily_pipeline import run_daily_pipeline
from autoscreener.pipeline_stages import (
    PIPELINE_STAGE_COUNT,
    PIPELINE_STAGE_SEQUENCE,
    RESERVED_STAGE_NUMBERS,
)


def test_pipeline_stage_sequence_is_unique_contiguous_and_matches_execution_order():
    sequences = list(PIPELINE_STAGE_SEQUENCE.values())
    assert len(sequences) == PIPELINE_STAGE_COUNT == len(set(sequences))
    assert sorted(sequences) == list(range(1, PIPELINE_STAGE_COUNT + 1))
    assert PIPELINE_STAGE_SEQUENCE["collection"] < PIPELINE_STAGE_SEQUENCE["consensus"]
    assert PIPELINE_STAGE_SEQUENCE["consensus"] < PIPELINE_STAGE_SEQUENCE["gates"]
    assert PIPELINE_STAGE_SEQUENCE["macro_exposure"] < PIPELINE_STAGE_SEQUENCE["model_v5_shadow"]
    assert PIPELINE_STAGE_SEQUENCE["model_v5_shadow"] < PIPELINE_STAGE_SEQUENCE["monitoring"]


def test_reserved_stage_numbers_never_double_count_pipeline_stage_count():
    """Audit fix (2026-09-03, Phase 7 re-review): a stage number reserved
    for code that is real but not yet wired into daily_pipeline.py's
    execution list must never inflate PIPELINE_STAGE_COUNT --
    frontend/src/pages/PipelinePage.tsx divides completed stages by this
    count, so an inflated count would make every future real run show a
    permanent, incorrect "N-1/N" shortfall."""
    assert "forward_validation_v5" not in PIPELINE_STAGE_SEQUENCE
    assert RESERVED_STAGE_NUMBERS["forward_validation_v5"] == PIPELINE_STAGE_COUNT + 1
    # No collision between a reserved number and an active stage number.
    assert set(RESERVED_STAGE_NUMBERS.values()).isdisjoint(PIPELINE_STAGE_SEQUENCE.values())


class _FakeRecorder:
    """`PipelineRecorder`(14.15、docs/daily_job_status_screen_2026-08-30.md)の
    実DBアクセスをこのテストファイルから隔離するダブル。

    このファイルの観点(月次/日次の骨格・順序・障害許容)は工程の記録内容とは
    無関係。既存の `session_scope` モックと同じ理由——記録自体の正しさは
    test_pipeline_recorder.py / test_pipeline_health.py が別途検証する。
    `stage()` は例外を一切飲み込まない素通しの contextmanager なので、
    「recorderは制御フローを変えない」という本実装の要件(§9)がテストの
    実行結果にも反映される。
    """

    def __init__(self, *args, **kwargs):
        pass

    @contextmanager
    def stage(self, name, sequence):
        handle = type("StageHandle", (), {"result": None})()
        yield handle

    def skip(self, name, sequence, reason):
        pass

    def non_core_failed_stages(self):
        return []

    def previous_scored(self):
        return None

    def finish(self, health):
        pass

    def prune_old_runs(self):
        return 0


@pytest.fixture(autouse=True)
def _stub_phase2367_steps():
    """第30章で追加した工程(EDGAR/FRED/モニタリング)と J-7 の需給工程は、この
    既存テストファイルの観点(月次/日次の骨格・順序・障害許容)とは無関係。
    パッチしないと実DB/実設定に依存する分岐に落ちてしまい、「本当は何もして
    いないのにtry/exceptで握りつぶされて緑になる」テストになる。
    自動適用のフィクスチャで明示的にモックし、`results`にキーが立つことだけ保証する。

    **2026-08-30:`collect_insider` / `collect_short_interest` の漏れを修正した。**
    J-7でこの2工程が `daily_pipeline` に足されたとき、このフィクスチャが更新されて
    いなかった。`EDGAR_USER_AGENT` が `.env` に設定されている環境では
    `collect_insider()` が**実物の `EdgarClient` を組み立て、最大300銘柄ぶんの
    Form 4 を実際にSECから取りに行っていた**(`collect_short_interest()` も同様に
    FINRAから実ダウンロードしていた)。結果、このファイルの6テストがそれぞれ
    数百回の実ネットワークアクセスを発生させ、テストスイートが数時間終わらず、
    SECにレート制限をかけられる原因になっていた。

    同じ漏れが J-6 の `collect_events`(yfinance のカレンダー取得)にもあった。

    **関数内で import される工程は、パッチ先が `daily_pipeline.*` ではなく
    定義元のモジュールでなければ効かない。** `daily_pipeline` の中で
    `from ... import x` している工程(events / insider / short_interest)は
    定義元を指すこと。工程を足すときは、それがネットワークかDBに触れるなら
    ここにも足す——ここを忘れても**テストは緑のまま、ただ遅くなるだけ**なので
    気づけない。
    """
    with (
        patch("autoscreener.batch.daily_pipeline.refresh_cik_map", return_value={"matched": 0}) as cik,
        patch("autoscreener.batch.daily_pipeline.collect_macro", return_value={"series": 0}) as macro,
        patch("autoscreener.batch.daily_pipeline.collect_filings", return_value={"tickers": 0}) as filings,
        patch("autoscreener.batch.daily_pipeline.collect_xbrl_facts", return_value={"tickers": 0}) as xbrl,
        patch("autoscreener.batch.daily_pipeline.collect_consensus", return_value={"tickers": 0}) as consensus,
        patch("autoscreener.batch.daily_pipeline.collect_investment_intelligence", return_value={"sections": 0}) as intelligence,
        # market_opportunity / macro_exposure も investment_intelligence と同じ
        # ライブ層の工程。ここに足し忘れていたため、両者が実物のまま走り
        # `select_tracked_tickers` の実DB経路 → 全追跡銘柄 × 全マクロ系列の
        # 週次回帰(`collect_macro_exposure` は価格系列を key ごとに再集計する
        # ので実質 O(n^2))に落ちて、このファイルのいくつかのテストが数分
        # 止まっていた(このフィクスチャの docstring が言う「緑のまま、ただ
        # 遅くなるだけ」の典型)。観点は骨格・順序・障害許容なので 0 件で通す。
        patch("autoscreener.batch.daily_pipeline.collect_market_opportunity", return_value={"targets": 0}) as market_opportunity,
        patch("autoscreener.batch.daily_pipeline.collect_macro_exposure", return_value={"targets": 0}) as macro_exposure,
        patch("autoscreener.batch.daily_pipeline.run_v5_shadow", return_value={"status": "succeeded", "population": 0}) as model_v5_shadow,
        patch("autoscreener.batch.daily_pipeline.collect_filing_sections", return_value={"sections": 0}) as filing_sections,
        patch("autoscreener.batch.daily_pipeline.collect_guidance", return_value={"rows": 0}) as guidance,
        patch("autoscreener.batch.daily_pipeline.collect_concentration", return_value={"rows": 0}) as concentration,
        patch("autoscreener.batch.daily_pipeline.collect_dilution", return_value={"rows": 0}) as dilution,
        patch("autoscreener.batch.daily_pipeline.collect_litigation", return_value={"rows": 0}) as litigation,
        patch("autoscreener.batch.daily_pipeline.run_monitoring", return_value={"tickers": 0}) as monitoring,
        patch(
            "autoscreener.batch.collect_events.collect_events", return_value={"tickers": 0}
        ) as events,
        patch(
            "autoscreener.batch.collect_supply.collect_insider", return_value={"tickers": 0}
        ) as insider,
        patch(
            "autoscreener.batch.collect_supply.collect_short_interest", return_value={"tickers": 0}
        ) as short_interest,
        patch("autoscreener.batch.daily_pipeline.PipelineRecorder", _FakeRecorder),
    ):
        yield {
            "cik": cik,
            "macro": macro,
            "filings": filings,
            "xbrl": xbrl,
            "consensus": consensus,
            "intelligence": intelligence,
            "market_opportunity": market_opportunity,
            "macro_exposure": macro_exposure,
            "model_v5_shadow": model_v5_shadow,
            "filing_sections": filing_sections,
            "guidance": guidance,
            "concentration": concentration,
            "dilution": dilution,
            "litigation": litigation,
            "monitoring": monitoring,
            "events": events,
            "insider": insider,
            "short_interest": short_interest,
        }


@patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.check_collection_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.run_backup")
@patch("autoscreener.batch.daily_pipeline.run_forward_validation", return_value={"computed": 0})
@patch("autoscreener.batch.daily_pipeline.run_scoring", return_value={"scored": 1})
@patch("autoscreener.batch.daily_pipeline.apply_gates", return_value={"included": 1})
@patch("autoscreener.batch.daily_pipeline.run_daily_collection", return_value={"success": 1})
@patch("autoscreener.batch.daily_pipeline.session_scope")
@patch("autoscreener.batch.daily_pipeline.run_backtest")
@patch("autoscreener.batch.daily_pipeline.refresh_universe", return_value=100)
@patch("autoscreener.batch.daily_pipeline.utc_today")
def test_monday_triggers_weekly_universe_refresh(
    mock_utc_today,
    mock_refresh,
    mock_backtest,
    mock_session_scope,
    mock_collect,
    mock_gates,
    mock_scoring,
    mock_forward,
    mock_backup,
    mock_check_collection,
    mock_check_quarantine,
):
    mock_utc_today.return_value = datetime.date(2026, 8, 24)  # Monday
    entered = mock_session_scope.return_value.__enter__.return_value
    entered.query.return_value.filter.return_value.all.return_value = []
    entered.query.return_value.filter.return_value.count.return_value = 0
    entered.query.return_value.count.return_value = 0

    results = run_daily_pipeline()

    mock_refresh.assert_called_once()
    assert "universe_refresh" in results
    assert results["universe_refresh"] == {"candidates": 100}
    mock_backtest.assert_called_once()


@patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.check_collection_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.run_backup")
@patch("autoscreener.batch.daily_pipeline.run_forward_validation", return_value={"computed": 0})
@patch("autoscreener.batch.daily_pipeline.run_scoring", return_value={"scored": 1})
@patch("autoscreener.batch.daily_pipeline.apply_gates", return_value={"included": 1})
@patch("autoscreener.batch.daily_pipeline.run_daily_collection", return_value={"success": 1})
@patch("autoscreener.batch.daily_pipeline.session_scope")
@patch("autoscreener.batch.daily_pipeline.run_backtest")
@patch("autoscreener.batch.daily_pipeline.refresh_universe", return_value=100)
@patch("autoscreener.batch.daily_pipeline.utc_today")
def test_non_monday_skips_universe_refresh(
    mock_utc_today,
    mock_refresh,
    mock_backtest,
    mock_session_scope,
    mock_collect,
    mock_gates,
    mock_scoring,
    mock_forward,
    mock_backup,
    mock_check_collection,
    mock_check_quarantine,
):
    mock_utc_today.return_value = datetime.date(2026, 8, 25)  # Tuesday
    entered = mock_session_scope.return_value.__enter__.return_value
    entered.query.return_value.filter.return_value.all.return_value = []
    entered.query.return_value.filter.return_value.count.return_value = 0
    entered.query.return_value.count.return_value = 0

    results = run_daily_pipeline()

    mock_refresh.assert_not_called()
    mock_backtest.assert_not_called()
    assert "universe_refresh" not in results


@patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.check_collection_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.run_backup")
@patch("autoscreener.batch.daily_pipeline.run_forward_validation", return_value={"computed": 3})
@patch("autoscreener.batch.daily_pipeline.run_scoring", return_value={"scored": 5})
@patch("autoscreener.batch.daily_pipeline.apply_gates", return_value={"included": 5})
@patch("autoscreener.batch.daily_pipeline.run_daily_collection", return_value={"success": 5})
@patch("autoscreener.batch.daily_pipeline.session_scope")
@patch("autoscreener.batch.daily_pipeline.utc_today")
def test_always_runs_collection_gates_scoring_and_forward_validation(
    mock_utc_today,
    mock_session_scope,
    mock_collect,
    mock_gates,
    mock_scoring,
    mock_forward,
    mock_backup,
    mock_check_collection,
    mock_check_quarantine,
):
    mock_utc_today.return_value = datetime.date(2026, 8, 25)  # Tuesday, no refresh
    entered = mock_session_scope.return_value.__enter__.return_value
    entered.query.return_value.filter.return_value.all.return_value = [("AAPL",), ("MSFT",)]
    entered.query.return_value.filter.return_value.count.return_value = 0
    entered.query.return_value.count.return_value = 2

    results = run_daily_pipeline()

    # collection_config はパイプラインが1度だけ読み、対象の選定(隔離銘柄の
    # 再挑戦期限判定)と収集本体の両方へ同じインスタンスを渡す。
    mock_collect.assert_called_once_with(
        ["AAPL", "MSFT"], collection_config=ANY, snapshot_date=datetime.date(2026, 8, 25)
    )
    mock_gates.assert_called_once_with(datetime.date(2026, 8, 25))
    mock_scoring.assert_called_once_with(datetime.date(2026, 8, 25))
    mock_forward.assert_called_once_with(datetime.date(2026, 8, 25))
    mock_backup.assert_called_once()
    mock_check_collection.assert_called_once_with({"success": 5})
    mock_check_quarantine.assert_called_once_with(0, 2)
    assert results["collection"] == {"success": 5}
    assert results["gates"] == {"included": 5}
    assert results["scoring"] == {"scored": 5}
    assert results["forward_validation"] == {"computed": 3}


@patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.check_collection_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.run_backup", side_effect=RuntimeError("docker unavailable"))
@patch("autoscreener.batch.daily_pipeline.run_forward_validation", return_value={"computed": 0})
@patch("autoscreener.batch.daily_pipeline.run_scoring", return_value={"scored": 5})
@patch("autoscreener.batch.daily_pipeline.apply_gates", return_value={"included": 5})
@patch("autoscreener.batch.daily_pipeline.run_daily_collection", return_value={"success": 5})
@patch("autoscreener.batch.daily_pipeline.session_scope")
@patch("autoscreener.batch.daily_pipeline.utc_today")
def test_backup_failure_does_not_crash_pipeline(
    mock_utc_today,
    mock_session_scope,
    mock_collect,
    mock_gates,
    mock_scoring,
    mock_forward,
    mock_backup,
    mock_check_collection,
    mock_check_quarantine,
):
    mock_utc_today.return_value = datetime.date(2026, 8, 25)
    entered = mock_session_scope.return_value.__enter__.return_value
    entered.query.return_value.filter.return_value.all.return_value = []
    entered.query.return_value.filter.return_value.count.return_value = 0
    entered.query.return_value.count.return_value = 0

    # バックアップが例外を投げても run_daily_pipeline 自体は正常終了する(18.4)
    results = run_daily_pipeline()
    assert results["scoring"] == {"scored": 5}


@patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.check_collection_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.run_backup")
@patch("autoscreener.batch.daily_pipeline.run_forward_validation", return_value={"computed": 0})
@patch("autoscreener.batch.daily_pipeline.run_scoring", return_value={"scored": 1})
@patch("autoscreener.batch.daily_pipeline.apply_gates", return_value={"included": 1})
@patch("autoscreener.batch.daily_pipeline.run_daily_collection", return_value={"success": 1})
@patch("autoscreener.batch.daily_pipeline.session_scope")
@patch("autoscreener.batch.daily_pipeline.run_backtest")
@patch("autoscreener.batch.daily_pipeline.refresh_universe", return_value=100)
@patch("autoscreener.batch.daily_pipeline.utc_today")
def test_weekly_backtest_runs_before_scoring(
    mock_utc_today,
    mock_refresh,
    mock_backtest,
    mock_session_scope,
    mock_collect,
    mock_gates,
    mock_scoring,
    mock_forward,
    mock_backup,
    mock_check_collection,
    mock_check_quarantine,
):
    """**順序が要件である**(28.8)。

    週次バックテストは確率の較正写像を学習し直す。スコアリングより後に走らせると、
    その週のスコアは1週間古い較正で書かれてしまう——実害が数字の水準にしか出ず、
    しかもUIには「較正済み」と表示されるので、壊れても気づけない種類のバグになる。
    """
    order: list[str] = []
    mock_backtest.side_effect = lambda *a, **k: order.append("backtest")
    mock_scoring.side_effect = lambda *a, **k: (order.append("scoring"), {"scored": 1})[1]

    mock_utc_today.return_value = datetime.date(2026, 8, 24)  # Monday
    entered = mock_session_scope.return_value.__enter__.return_value
    entered.query.return_value.filter.return_value.all.return_value = []
    entered.query.return_value.filter.return_value.count.return_value = 0
    entered.query.return_value.count.return_value = 0

    run_daily_pipeline()

    assert order == ["backtest", "scoring"]


@patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.check_collection_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.run_backup")
@patch("autoscreener.batch.daily_pipeline.run_forward_validation", return_value={"computed": 0})
@patch("autoscreener.batch.daily_pipeline.run_scoring", return_value={"scored": 1})
@patch("autoscreener.batch.daily_pipeline.apply_gates", return_value={"included": 1})
@patch("autoscreener.batch.daily_pipeline.run_daily_collection", return_value={"success": 1})
@patch("autoscreener.batch.daily_pipeline.session_scope")
@patch("autoscreener.batch.daily_pipeline.run_backtest", side_effect=RuntimeError("boom"))
@patch("autoscreener.batch.daily_pipeline.refresh_universe", return_value=100)
@patch("autoscreener.batch.daily_pipeline.utc_today")
def test_backtest_failure_does_not_stop_the_pipeline(
    mock_utc_today,
    mock_refresh,
    mock_backtest,
    mock_session_scope,
    mock_collect,
    mock_gates,
    mock_scoring,
    mock_forward,
    mock_backup,
    mock_check_collection,
    mock_check_quarantine,
):
    """較正に失敗しても当日のスコア計算は続ける(18.4の縮退運用)。

    較正が無ければスコアは未較正のまま保存され、UIがその状態を明示する。
    「較正できなかった」ことを理由にランキングごと失わせる理由はない。
    """
    mock_utc_today.return_value = datetime.date(2026, 8, 24)  # Monday
    entered = mock_session_scope.return_value.__enter__.return_value
    entered.query.return_value.filter.return_value.all.return_value = []
    entered.query.return_value.filter.return_value.count.return_value = 0
    entered.query.return_value.count.return_value = 0

    results = run_daily_pipeline()

    mock_scoring.assert_called_once()
    assert "backtest" not in results
    assert results["scoring"] == {"scored": 1}
