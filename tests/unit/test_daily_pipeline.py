import datetime
from contextlib import contextmanager
from unittest.mock import ANY, patch

import pytest

from autoscreener.batch.daily_pipeline import run_daily_pipeline
from autoscreener.batch.market_session import MarketSessionDecision
from autoscreener.batch.pipeline_recorder import PipelineRecorder as RealPipelineRecorder
from autoscreener.db.models import PipelineRun, PipelineStageRun
from autoscreener.db.session import session_scope as real_session_scope
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
    # A-4 (2026-09-04, docs/racr_wp_a_operational_safety_2026-09-04.md):
    # forward_validation_v5 runs after model_v5_shadow in real time, but its
    # number (26) is higher than monitoring (24) and backup (25) because
    # existing stage numbers are never renumbered. This test's name says
    # "matches execution order" -- forward_validation_v5 is the one
    # deliberate exception to that, so it is asserted on its own rather
    # than folded into the general contiguous-and-increasing checks above.
    assert PIPELINE_STAGE_SEQUENCE["model_v5_shadow"] < PIPELINE_STAGE_SEQUENCE["forward_validation_v5"]


def test_forward_validation_v5_wired_into_stage_sequence():
    """A-4(2026-09-04、docs/racr_wp_a_operational_safety_2026-09-04.md、
    監査§10.1/10.4)。`forward_validation_v5` はPhase 7で26番として予約
    (`RESERVED_STAGE_NUMBERS`)されたが `daily_pipeline.py` へは未配線
    だった。A-4がそれを配線したので、この旧テスト
    (`test_reserved_stage_numbers_never_double_count_pipeline_stage_count`、
    「予約されたまま」を検証していた)を配線後の状態を検証するテストへ
    置き換える。
    """
    assert PIPELINE_STAGE_SEQUENCE["forward_validation_v5"] == 26
    assert PIPELINE_STAGE_COUNT == 26
    # 予約は空になった(移した先のforward_validation_v5がこの辞書に残る
    # 唯一のエントリだったため)。削除ではなく空のまま残す方針
    # (pipeline_stages.py参照)。
    assert RESERVED_STAGE_NUMBERS == {}
    assert sorted(PIPELINE_STAGE_SEQUENCE.values()) == list(range(1, PIPELINE_STAGE_COUNT + 1))


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

    def resumed_stage_results(self):
        # A-6(2026-09-04):このファイルの他テストは resume=False の既定経路
        # (新規run)しか通らないため、常に空でよい——resumeそのものの検証は
        # test_pipeline_recorder.py(実DB)が別途行う。
        return {}

    def finish_with_exception(self, exc):
        # A-2(2026-09-04):outer try/exceptから呼ばれる。DBに触れないダブル
        # なので何もしない——実DBでの確定挙動は
        # test_core_stage_exception_does_not_leave_run_status_running が
        # 実物の PipelineRecorder で別途検証する。
        pass

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

    **2026-09-04(A-2/A-4追加):`sweep_orphan_runs` / `run_forward_validation_v5`
    も足した。** 前者は `run_daily_pipeline()` の冒頭で無条件に呼ばれる
    ようになった関数で、実物のままだと(このファイルの他の工程とは異なり)
    `PipelineRecorder` を経由せず直接 `pipeline_recorder.session_scope()` を
    叩く——`_FakeRecorder` へのpatchでは防げない実DBアクセスの経路になる。
    後者(A-4で新たに配線したv5 forward validation)も同様に実DBへ触れる。
    どちらもこのファイルの観点(骨格・順序・障害許容)とは無関係なので、
    他のv5工程(`model_v5_shadow`等)と同じく0件で通す。

    **関数内で import される工程は、パッチ先が `daily_pipeline.*` ではなく
    定義元のモジュールでなければ効かない。** `daily_pipeline` の中で
    `from ... import x` している工程(events / insider / short_interest)は
    定義元を指すこと。工程を足すときは、それがネットワークかDBに触れるなら
    ここにも足す——ここを忘れても**テストは緑のまま、ただ遅くなるだけ**なので
    気づけない。
    """
    # 2026-09-04(A-2/A-4追加でパッチが20件を超えた):CPythonの構文コンパイラは
    # 括弧付き `with (a, b, ...)` を静的に入れ子の `with` として展開するため、
    # 一定数を超えると `SyntaxError: too many statically nested blocks` になる。
    # `ExitStack` で動的に積む形へ変える——挙動(全パッチを1つのブロックで
    # 有効化し、fixtureのteardownで一括解除)は変えていない。
    from contextlib import ExitStack

    with ExitStack() as stack:
        cik = stack.enter_context(patch("autoscreener.batch.daily_pipeline.refresh_cik_map", return_value={"matched": 0}))
        macro = stack.enter_context(patch("autoscreener.batch.daily_pipeline.collect_macro", return_value={"series": 0}))
        filings = stack.enter_context(patch("autoscreener.batch.daily_pipeline.collect_filings", return_value={"tickers": 0}))
        xbrl = stack.enter_context(patch("autoscreener.batch.daily_pipeline.collect_xbrl_facts", return_value={"tickers": 0}))
        consensus = stack.enter_context(patch("autoscreener.batch.daily_pipeline.collect_consensus", return_value={"tickers": 0}))
        intelligence = stack.enter_context(
            patch("autoscreener.batch.daily_pipeline.collect_investment_intelligence", return_value={"sections": 0})
        )
        # market_opportunity / macro_exposure も investment_intelligence と同じ
        # ライブ層の工程。ここに足し忘れていたため、両者が実物のまま走り
        # `select_tracked_tickers` の実DB経路 → 全追跡銘柄 × 全マクロ系列の
        # 週次回帰(`collect_macro_exposure` は価格系列を key ごとに再集計する
        # ので実質 O(n^2))に落ちて、このファイルのいくつかのテストが数分
        # 止まっていた(このフィクスチャの docstring が言う「緑のまま、ただ
        # 遅くなるだけ」の典型)。観点は骨格・順序・障害許容なので 0 件で通す。
        market_opportunity = stack.enter_context(
            patch("autoscreener.batch.daily_pipeline.collect_market_opportunity", return_value={"targets": 0})
        )
        macro_exposure = stack.enter_context(
            patch("autoscreener.batch.daily_pipeline.collect_macro_exposure", return_value={"targets": 0})
        )
        model_v5_shadow = stack.enter_context(
            patch(
                "autoscreener.batch.daily_pipeline.run_v5_shadow",
                return_value={"status": "succeeded", "population": 0},
            )
        )
        forward_validation_v5 = stack.enter_context(
            patch("autoscreener.batch.daily_pipeline.run_forward_validation_v5", return_value={"computed": 0})
        )
        orphan_sweep = stack.enter_context(patch("autoscreener.batch.daily_pipeline.sweep_orphan_runs", return_value=[]))
        market_session = stack.enter_context(patch(
            "autoscreener.batch.daily_pipeline.assess_market_session",
            return_value=MarketSessionDecision(
                should_run=True,
                expected_session=datetime.date(2026, 8, 24),
                latest_covered_session=None,
                covered_count=0,
                target_count=0,
            ),
        ))
        filing_sections = stack.enter_context(
            patch("autoscreener.batch.daily_pipeline.collect_filing_sections", return_value={"sections": 0})
        )
        guidance = stack.enter_context(patch("autoscreener.batch.daily_pipeline.collect_guidance", return_value={"rows": 0}))
        concentration = stack.enter_context(
            patch("autoscreener.batch.daily_pipeline.collect_concentration", return_value={"rows": 0})
        )
        dilution = stack.enter_context(patch("autoscreener.batch.daily_pipeline.collect_dilution", return_value={"rows": 0}))
        litigation = stack.enter_context(
            patch("autoscreener.batch.daily_pipeline.collect_litigation", return_value={"rows": 0})
        )
        monitoring = stack.enter_context(patch("autoscreener.batch.daily_pipeline.run_monitoring", return_value={"tickers": 0}))
        events = stack.enter_context(
            patch("autoscreener.batch.collect_events.collect_events", return_value={"tickers": 0})
        )
        insider = stack.enter_context(
            patch("autoscreener.batch.collect_supply.collect_insider", return_value={"tickers": 0})
        )
        short_interest = stack.enter_context(
            patch("autoscreener.batch.collect_supply.collect_short_interest", return_value={"tickers": 0})
        )
        stack.enter_context(patch("autoscreener.batch.daily_pipeline.PipelineRecorder", _FakeRecorder))

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
            "forward_validation_v5": forward_validation_v5,
            "orphan_sweep": orphan_sweep,
            "market_session": market_session,
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
        ["AAPL", "MSFT"],
        collection_config=ANY,
        snapshot_date=datetime.date(2026, 8, 25),
        market_session_date=datetime.date(2026, 8, 24),
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


@patch("autoscreener.batch.daily_pipeline.utc_today", return_value=datetime.date(2026, 9, 8))
def test_completed_market_session_skips_market_stages_without_health_error(
    mock_utc_today, _stub_phase2367_steps
):
    _stub_phase2367_steps["market_session"].return_value = MarketSessionDecision(
        should_run=False,
        expected_session=datetime.date(2026, 9, 4),
        latest_covered_session=datetime.date(2026, 9, 4),
        covered_count=1,
        target_count=1,
        reason="no_new_market_session",
    )
    with (
        patch("autoscreener.batch.daily_pipeline.select_collectable_symbols", return_value=["AAPL"]),
        patch("autoscreener.batch.daily_pipeline.collection_population_counts", return_value=(0, 1)),
        patch("autoscreener.batch.daily_pipeline.select_tracked_tickers", return_value=[]),
        patch("autoscreener.batch.daily_pipeline.run_daily_collection") as collection,
        patch("autoscreener.batch.daily_pipeline.apply_gates") as gates,
        patch("autoscreener.batch.daily_pipeline.run_scoring") as scoring,
        patch("autoscreener.batch.daily_pipeline.run_forward_validation") as forward,
        patch("autoscreener.batch.daily_pipeline.run_backup"),
        patch("autoscreener.batch.daily_pipeline.check_pipeline_health", return_value=[]) as health_check,
        patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[]),
    ):
        results = run_daily_pipeline()

    collection.assert_not_called()
    gates.assert_not_called()
    scoring.assert_not_called()
    forward.assert_not_called()
    _stub_phase2367_steps["consensus"].assert_not_called()
    assert results["collection"]["skipped_reason"] == "no_new_market_session"
    assert results["scoring"]["skipped_reason"] == "no_new_market_session"
    assert health_check.call_args.kwargs["scoring_result"] is None


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


@patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.check_collection_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.apply_gates", side_effect=RuntimeError("FK violation (simulated 2026-09-03)"))
@patch("autoscreener.batch.daily_pipeline.run_daily_collection", return_value={"success": 1})
@patch("autoscreener.batch.daily_pipeline.session_scope")
@patch("autoscreener.batch.daily_pipeline.utc_today")
def test_core_stage_exception_does_not_leave_run_status_running(
    mock_utc_today,
    mock_session_scope,
    mock_collect,
    mock_gates,
    mock_check_collection,
    mock_check_quarantine,
):
    """A-2の受け入れ条件(docs/racr_wp_a_operational_safety_2026-09-04.md、
    監査§10.2/10.3):core stageが例外を投げても `pipeline_runs.status` が
    `running` のまま残らないこと。

    2026-09-03の実障害の直接再現——gate stageのFK違反で `run_daily_pipeline()`
    が例外を送出したとき、outer try/finally が無かったため
    `recorder.finish()` に到達せず、runが `running` のまま永久に残った。

    このファイルの他テストが使う `_FakeRecorder`(DBに触れないダブル)では
    「DBの行がどう確定するか」を検証できないため、このテストだけ実物の
    `PipelineRecorder` に差し替え、専用テストDB(`TEST_DATABASE_URL`)の
    実際の行を確認する。
    """
    mock_utc_today.return_value = datetime.date(2026, 8, 25)  # Tuesday, no weekly stages
    entered = mock_session_scope.return_value.__enter__.return_value
    entered.query.return_value.filter.return_value.all.return_value = []
    entered.query.return_value.filter.return_value.count.return_value = 0
    entered.query.return_value.count.return_value = 0

    captured: dict[str, RealPipelineRecorder] = {}

    class _CapturingRecorder(RealPipelineRecorder):
        """実物の `PipelineRecorder` をそのまま使いつつ、生成された
        インスタンス(run_id)をテスト側から参照できるようにするだけの薄い
        ラッパー。挙動は一切変えない。"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["recorder"] = self

    try:
        with patch("autoscreener.batch.daily_pipeline.PipelineRecorder", _CapturingRecorder):
            with pytest.raises(RuntimeError, match="FK violation"):
                run_daily_pipeline()

        recorder = captured["recorder"]
        with real_session_scope() as session:
            row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
            assert row.status == "failed"
            assert row.status != "running"
            assert row.finished_at is not None
            codes = [h["code"] for h in (row.health or [])]
            assert "run_unhandled_exception" in codes
    finally:
        if "recorder" in captured:
            with real_session_scope() as session:
                session.query(PipelineRun).filter_by(run_id=captured["recorder"].run_id).delete()


@patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.check_collection_health", return_value=[])
@patch("autoscreener.batch.daily_pipeline.run_backup")
@patch("autoscreener.batch.daily_pipeline.run_forward_validation", return_value={"computed": 1})
@patch("autoscreener.batch.daily_pipeline.run_scoring", return_value={"scored": 1})
@patch("autoscreener.batch.daily_pipeline.apply_gates", return_value={"included": 1})
@patch("autoscreener.batch.daily_pipeline.run_daily_collection")
@patch("autoscreener.batch.daily_pipeline.utc_today")
def test_resume_does_not_redo_an_already_succeeded_expensive_stage(
    mock_utc_today,
    mock_collect,
    mock_gates,
    mock_scoring,
    mock_forward,
    mock_backup,
    mock_check_collection,
    mock_check_quarantine,
    _stub_phase2367_steps,
):
    """A-6の受け入れ条件(docs/racr_wp_a_operational_safety_2026-09-04.md、
    監査§10.3「2時間超のcollection後にgateで落ちても、checkpoint/resumeが
    無い」):`run_daily_pipeline(resume=True)` は、前回succeededした工程
    (ここでは`collection`)を再実行しない。

    `session_scope` はこのテストではモックしない(専用テストDBに対して
    実際にクエリさせる)——`_find_resumable_run_id` が
    `daily_pipeline.session_scope` を通じて前回runを検索するため、そこを
    モックすると検索そのものが機能しなくなる。他の重い工程は
    `_stub_phase2367_steps`(このファイルのautouseフィクスチャ)がモック済み。
    """
    resume_date = datetime.date(2026, 8, 26)  # Wednesday, no weekly stages
    mock_utc_today.return_value = resume_date
    # The succeeded collection can make the DB look current on resume.  That
    # must suppress only a second collection, not unfinished downstream work.
    _stub_phase2367_steps["market_session"].return_value = MarketSessionDecision(
        should_run=False,
        expected_session=datetime.date(2026, 8, 25),
        latest_covered_session=datetime.date(2026, 8, 25),
        covered_count=999,
        target_count=999,
        reason="no_new_market_session",
    )

    # 前回の途中失敗runを再現する:collectionはsucceeded、gatesはfailed。
    pre_recorder = RealPipelineRecorder(resume_date, is_weekly=False)
    with pre_recorder.stage("collection", PIPELINE_STAGE_SEQUENCE["collection"]) as st:
        st.result = {"success": 999, "quarantined": 0, "universe_size": 0}
    with pytest.raises(RuntimeError):
        with pre_recorder.stage("gates", PIPELINE_STAGE_SEQUENCE["gates"]) as st:
            raise RuntimeError("FK violation (simulated 2026-09-03)")
    pre_recorder.finish_with_exception(RuntimeError("FK violation (simulated 2026-09-03)"))

    captured: dict[str, RealPipelineRecorder] = {}

    class _CapturingRecorder(RealPipelineRecorder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["recorder"] = self

    try:
        with patch("autoscreener.batch.daily_pipeline.PipelineRecorder", _CapturingRecorder):
            results = run_daily_pipeline(resume=True)

        # 核心:前回succeededした collection の実処理(`run_daily_collection`)
        # は一度も呼ばれない。
        mock_collect.assert_not_called()
        # それでも結果は前回の値がそのまま引き継がれている(捏造した0件では
        # なく、実際に前回計算された999件)。
        assert results["collection"]["success"] == 999

        # 同じrun_idへ合流している(新しいrunを作っていない)。
        assert captured["recorder"].run_id == pre_recorder.run_id

        # gates(前回failed)は今回再試行され、succeededに上書きされている。
        mock_gates.assert_called_once()
        with real_session_scope() as session:
            gates_row = (
                session.query(PipelineStageRun)
                .filter_by(run_id=pre_recorder.run_id, stage="gates")
                .one()
            )
            assert gates_row.status == "succeeded"
            assert gates_row.result == {"included": 1}
            assert gates_row.error_message is None

            run_row = session.query(PipelineRun).filter_by(run_id=pre_recorder.run_id).one()
            assert run_row.status == "succeeded"
    finally:
        with real_session_scope() as session:
            session.query(PipelineRun).filter_by(run_id=pre_recorder.run_id).delete()


@patch("autoscreener.batch.daily_pipeline.utc_today")
def test_resume_without_an_incomplete_run_starts_fresh(mock_utc_today):
    """A-6:`--resume` を付けても、その日の未完走runが無ければ通常どおり
    新規runになる(安全側のフォールバック。誤って古いrunへ合流しない)。"""
    resume_date = datetime.date(2026, 8, 27)  # Thursday, arbitrary and unused elsewhere
    mock_utc_today.return_value = resume_date

    with real_session_scope() as session:
        session.query(PipelineRun).filter(PipelineRun.run_date == resume_date).delete()

    captured: dict[str, RealPipelineRecorder] = {}

    class _CapturingRecorder(RealPipelineRecorder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["recorder"] = self

    with (
        patch("autoscreener.batch.daily_pipeline.PipelineRecorder", _CapturingRecorder),
        patch("autoscreener.batch.daily_pipeline.run_daily_collection", return_value={"success": 0}),
        patch("autoscreener.batch.daily_pipeline.apply_gates", return_value={"included": 0}),
        patch("autoscreener.batch.daily_pipeline.run_scoring", return_value={"scored": 0}),
        patch("autoscreener.batch.daily_pipeline.run_forward_validation", return_value={"computed": 0}),
        patch("autoscreener.batch.daily_pipeline.run_backup"),
        patch("autoscreener.batch.daily_pipeline.check_collection_health", return_value=[]),
        patch("autoscreener.batch.daily_pipeline.check_quarantine_health", return_value=[]),
    ):
        try:
            run_daily_pipeline(resume=True)
            assert "recorder" in captured
            # 新規run_idが払い出されている(既存runへ合流していない)。
            with real_session_scope() as session:
                count = (
                    session.query(PipelineRun)
                    .filter(PipelineRun.run_date == resume_date)
                    .count()
                )
                assert count == 1
        finally:
            with real_session_scope() as session:
                session.query(PipelineRun).filter(PipelineRun.run_date == resume_date).delete()
