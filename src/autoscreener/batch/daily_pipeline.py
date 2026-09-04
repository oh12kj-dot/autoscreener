"""日次パイプライン:収集 → ゲート適用 → スコアリングを1回の実行でまとめる
(5.2の取得頻度、17章の実装順序)。スケジューラ(Windows Task Scheduler等、
3章)から1日1回呼び出される想定のエントリポイント。

ユニバースの再取得(5.2:週次)は月曜日のみ実行し、それ以外の曜日は既存の
`tickers` テーブルに対して収集・ゲート適用・スコアリングを行う。

**擬似バックテストも週次で回す(28.8)。** 確率の較正写像はバックテストの
観測から学習されるが、価格ヒストリーは日々伸びるので、放置すると較正が
古い観測に固定されたままになる。月曜に**スコアリングより先に**回すことで、
その週のスコアは最新の観測で較正された確率を持つ。

**2026-08-30(docs/daily_job_status_screen_2026-08-30.md、14.15の運用監視):**
`PipelineRecorder` で工程ごとの実行を `pipeline_runs` / `pipeline_stage_runs`
に記録するようにした。**既存のtry/except構造(どの工程が全体を止め、
どの工程が握り潰すか)は一切変えていない**——記録は副作用として追加した
だけで、失敗時の制御フローは08-29の実運用当時のままである(§9)。唯一の
例外が insider/short_interest のtry分割(§3.5※、下記コメント参照)。
"""

from __future__ import annotations

import logging

from autoscreener.backtest.runner import run_backtest
from autoscreener.batch.apply_gates import apply_gates
from autoscreener.batch.backup import run_backup
from autoscreener.batch.collect_filings import collect_filings, select_tracked_tickers
from autoscreener.batch.collect_macro import collect_macro
from autoscreener.batch.collect_macro_exposure import collect_macro_exposure
from autoscreener.batch.collect_market_opportunity import collect_market_opportunity
from autoscreener.batch.collect_xbrl_facts import collect_xbrl_facts
from autoscreener.batch.collect_consensus import collect_consensus
from autoscreener.batch.collect_investment_intelligence import collect_investment_intelligence
from autoscreener.batch.collect_filing_sections import collect_filing_sections
from autoscreener.batch.collect_guidance import collect_guidance
from autoscreener.batch.collect_concentration import collect_concentration
from autoscreener.batch.collect_dilution import collect_dilution
from autoscreener.batch.collect_litigation import collect_litigation
from autoscreener.batch.pipeline_recorder import PipelineRecorder
from autoscreener.batch.refresh_cik_map import refresh_cik_map
from autoscreener.batch.run_daily_collection import (
    collection_population_counts,
    run_daily_collection,
    select_collectable_symbols,
)
from autoscreener.batch.run_monitoring import run_monitoring
from autoscreener.batch.universe_refresh import refresh_universe
from autoscreener.config import load_collection_config, load_edgar_config
from autoscreener.dates import WEEKLY_REFRESH_WEEKDAY, utc_today
from autoscreener.db.session import session_scope
from autoscreener.monitoring import HealthFinding, check_collection_health, check_pipeline_health, check_quarantine_health
from autoscreener.pipeline_stages import PIPELINE_STAGE_SEQUENCE
from autoscreener.scoring.engine import run_scoring
from autoscreener.scoring.v5.engine import run_v5_shadow
from autoscreener.scoring.forward_validation import run_forward_validation

logger = logging.getLogger(__name__)


def run_daily_pipeline() -> dict[str, dict[str, int]]:
    today = utc_today()
    results: dict[str, dict[str, int]] = {}
    is_weekly = today.weekday() == WEEKLY_REFRESH_WEEKDAY
    # 14.15:工程ごとの実行記録。トリガー種別(scheduled/manual)を区別する
    # 呼び出し経路が現状1つ(CLI)しか無いため、既定値のまま固定する。
    recorder = PipelineRecorder(today, is_weekly)
    health: list[HealthFinding] = []

    if is_weekly:
        logger.info("weekly universe refresh (weekday=%s)", today.weekday())
        with recorder.stage("universe_refresh", PIPELINE_STAGE_SEQUENCE["universe_refresh"]) as st:
            count = refresh_universe(today)
            st.result = results["universe_refresh"] = {"candidates": count}

        # 30.3.2:CIK突合もユニバース再取得と同じ週次サイクルで回す(新規上場銘柄の
        # CIKを取り込むため)。EDGAR_USER_AGENT未設定の環境(30.3.1)ではEDGAR連携
        # 全体を使わない選択をしている利用者がいるため、失敗はログに残しつつ
        # パイプライン全体は止めない(backupと同じ扱い)。
        logger.info("weekly CIK map refresh")
        try:
            with recorder.stage("cik_map_refresh", PIPELINE_STAGE_SEQUENCE["cik_map_refresh"]) as st:
                st.result = results["cik_map_refresh"] = refresh_cik_map()
        except Exception:
            logger.exception("weekly CIK map refresh failed (EDGAR_USER_AGENT not set?)")

        # 30.8.2:財務データと同じく、マクロ系列も日々変わるものではないので週次で足りる。
        logger.info("weekly macro collection")
        try:
            with recorder.stage("macro", PIPELINE_STAGE_SEQUENCE["macro"]) as st:
                st.result = results["macro"] = collect_macro()
        except Exception:
            logger.exception("weekly macro collection failed (FRED_API_KEY not set?)")

        # 30.5.5:XBRL実績値も財務データなので四半期に1回しか変わらない。週次で足りる。
        logger.info("weekly XBRL facts collection")
        try:
            with recorder.stage("xbrl_facts", PIPELINE_STAGE_SEQUENCE["xbrl_facts"]) as st:
                st.result = results["xbrl_facts"] = collect_xbrl_facts()
        except Exception:
            logger.exception("weekly XBRL facts collection failed (EDGAR_USER_AGENT not set?)")

        # J-6(docs/investment_decision_gap_2026-08-29.md):次回決算日の収集。yfinance の
        # スナップショットしか取れずレート制限も食うので、追跡対象のみ・週次で足りる。
        # 失敗してもパイプラインは止めない。
        logger.info("weekly event calendar collection")
        try:
            from autoscreener.batch.collect_events import collect_events

            with recorder.stage("events", PIPELINE_STAGE_SEQUENCE["events"]) as st:
                st.result = results["events"] = collect_events()
        except Exception:
            logger.exception("weekly event calendar collection failed")

        # J-7:需給(Form 4・空売り残)。原則3:ゲート・スコアには入れない。
        # 取得経路(EDGAR/FINRA)が未整備の間は 0 件で通る。
        #
        # §3.5※(docs/daily_job_status_screen_2026-08-30.md):以前は insider と
        # short_interest が単一の try を共有しており、collect_insider() が
        # 落ちると collect_short_interest() が実行されないのに、ログ上は
        # 両方が一括で失敗したようにしか見えなかった。工程単位で記録する以上、
        # この2つを判別できないのは記録の正確さを損なう。挙動の改善だが、
        # 記録の正確さのために必要な変更として2つのtryに分割する。
        logger.info("weekly insider transactions collection")
        try:
            from autoscreener.batch.collect_supply import collect_insider

            with recorder.stage("insider", PIPELINE_STAGE_SEQUENCE["insider"]) as st:
                st.result = results["insider"] = collect_insider()
        except Exception:
            logger.exception("weekly insider transactions collection failed")

        logger.info("weekly short interest collection")
        try:
            from autoscreener.batch.collect_supply import collect_short_interest

            with recorder.stage("short_interest", PIPELINE_STAGE_SEQUENCE["short_interest"]) as st:
                st.result = results["short_interest"] = collect_short_interest()
        except Exception:
            logger.exception("weekly short interest collection failed")
    else:
        # 火〜日は週次工程の対象外(§3.3)。`skipped` を `failed` と混ぜないための
        # 明示的な記録——これが無いと画面が毎日8件の「失敗」を出し、誰も見なく
        # なる。
        recorder.skip("universe_refresh", PIPELINE_STAGE_SEQUENCE["universe_refresh"], "not_weekly")
        recorder.skip("cik_map_refresh", PIPELINE_STAGE_SEQUENCE["cik_map_refresh"], "not_weekly")
        recorder.skip("macro", PIPELINE_STAGE_SEQUENCE["macro"], "not_weekly")
        recorder.skip("xbrl_facts", PIPELINE_STAGE_SEQUENCE["xbrl_facts"], "not_weekly")
        recorder.skip("events", PIPELINE_STAGE_SEQUENCE["events"], "not_weekly")
        recorder.skip("insider", PIPELINE_STAGE_SEQUENCE["insider"], "not_weekly")
        recorder.skip("short_interest", PIPELINE_STAGE_SEQUENCE["short_interest"], "not_weekly")

    # 18.1:隔離中でも再挑戦期限が来た銘柄は対象に含める(`select_collectable_symbols`)。
    # 設定を1度だけ読んで収集本体にも渡す——対象の選定と `collect_one` の再挑戦判定が
    # 別々の設定インスタンスを見ると、両者の閾値がずれうるため。
    collection_config = load_collection_config()
    with session_scope() as session:
        symbols = select_collectable_symbols(session, collection_config)

    logger.info("daily collection: %d symbols", len(symbols))
    with recorder.stage("collection", PIPELINE_STAGE_SEQUENCE["collection"]) as st:
        results["collection"] = run_daily_collection(
            symbols, collection_config=collection_config, snapshot_date=today
        )
        # 18.1/18.7:隔離状態は収集工程(`collect_one`)でのみ更新され、以降の
        # 工程では変わらない。ここで1度だけ読み、隔離健全性判定と画面の
        # 「隔離率」タイル(§6.3)の両方に使う。`results["collection"]`(戻り値・
        # CLI出力に使われる)自体は汚さず、記録用の `st.result` にだけ添える。
        with session_scope() as session:
            quarantined_count, universe_size = collection_population_counts(session)
        st.result = {**results["collection"], "quarantined": quarantined_count, "universe_size": universe_size}
    health.extend(check_collection_health(results["collection"]))  # 18.7

    # TENX v2: append-only current consensus. This is a display/PIT-history
    # layer and failures must not prevent the deterministic core score.
    logger.info("collecting analyst consensus snapshots")
    try:
        with recorder.stage("consensus", PIPELINE_STAGE_SEQUENCE["consensus"]) as st:
            st.result = results["consensus"] = collect_consensus()
    except Exception:
        logger.exception("consensus collection failed")

    logger.info("applying gates")
    with recorder.stage("gates", PIPELINE_STAGE_SEQUENCE["gates"]) as st:
        st.result = results["gates"] = apply_gates(today)

    if is_weekly:
        # 28.8:較正写像を最新の観測で学習し直す。**スコアリングより前**に
        # 実行するのが要点——順序が逆だと、その週のスコアは1週間古い較正で
        # 書かれる。失敗してもパイプライン全体は止めない(較正が無ければ
        # スコアは未較正のまま保存され、UIがその状態を明示する)。
        logger.info("weekly backtest (recalibration)")
        try:
            with recorder.stage("backtest", PIPELINE_STAGE_SEQUENCE["backtest"]) as st:
                metrics = run_backtest()
                st.result = results["backtest"] = {"observations": metrics.observation_count}
        except Exception:
            logger.exception("weekly backtest failed — scores will use the previous calibration map")
    else:
        recorder.skip("backtest", PIPELINE_STAGE_SEQUENCE["backtest"], "not_weekly")

    logger.info("running scoring")
    with recorder.stage("scoring", PIPELINE_STAGE_SEQUENCE["scoring"]) as st:
        st.result = results["scoring"] = run_scoring(today)

    logger.info("running forward validation")
    with recorder.stage("forward_validation", PIPELINE_STAGE_SEQUENCE["forward_validation"]) as st:
        st.result = results["forward_validation"] = run_forward_validation(today)

    # 30.3.6:追跡対象の選定に当日のランキングを使うため、スコアが確定してから
    # (run_scoringの後)実行する。EDGAR_USER_AGENT未設定なら EdgarClient が
    # ValueError で落ちるが、それでパイプライン全体を止めない(バックアップと
    # 同じ扱い)——EDGAR連携は本計画のオプション機能であり、未設定は運用上の
    # 正常状態でありうる。
    # Freeze this target universe once so every filing-derived stage and its
    # coverage ledger describe the same population.
    with session_scope() as session:
        tracked_symbols = [ticker.symbol for ticker in select_tracked_tickers(
            session, limit=load_edgar_config().max_tracked_tickers
        )]
    logger.info("collecting SEC filings for %d frozen tracked tickers", len(tracked_symbols))
    try:
        with recorder.stage("filings", PIPELINE_STAGE_SEQUENCE["filings"]) as st:
            st.result = results["filings"] = collect_filings(symbols=tracked_symbols)
    except Exception:
        logger.exception("collect_filings failed (EDGAR_USER_AGENT not set, or SEC unavailable?)")

    logger.info("extracting source sections from new SEC filings")
    try:
        with recorder.stage("filing_sections", PIPELINE_STAGE_SEQUENCE["filing_sections"]) as st:
            st.result = results["filing_sections"] = collect_filing_sections(symbols=tracked_symbols)
    except Exception:
        logger.exception("filing section extraction failed")

    disclosure_jobs = (
        ("guidance", collect_guidance),
        ("customer_concentration", collect_concentration),
        ("dilution", collect_dilution),
        ("litigation", collect_litigation),
    )
    for stage_name, job in disclosure_jobs:
        try:
            with recorder.stage(stage_name, PIPELINE_STAGE_SEQUENCE[stage_name]) as st:
                st.result = results[stage_name] = job(symbols=tracked_symbols)
        except Exception:
            logger.exception("%s extraction failed", stage_name)

    logger.info("extracting investment intelligence from stored filing sections")
    try:
        with recorder.stage("investment_intelligence", PIPELINE_STAGE_SEQUENCE["investment_intelligence"]) as st:
            st.result = results["investment_intelligence"] = collect_investment_intelligence(symbols=tracked_symbols)
    except Exception:
        logger.exception("investment intelligence extraction failed")

    try:
        with recorder.stage("market_opportunity", PIPELINE_STAGE_SEQUENCE["market_opportunity"]) as st:
            st.result = results["market_opportunity"] = collect_market_opportunity(symbols=tracked_symbols)
    except Exception:
        logger.exception("market opportunity collection failed")

    try:
        with recorder.stage("macro_exposure", PIPELINE_STAGE_SEQUENCE["macro_exposure"]) as st:
            st.result = results["macro_exposure"] = collect_macro_exposure(symbols=tracked_symbols)
    except Exception:
        logger.exception("macro exposure calculation failed")

    # Issue #3 Phase 1: v5 is append-only and shadow-only.  A failure is
    # recorded as a non-core failed stage but never prevents v4 scoring or the
    # remaining operational stages from completing.
    try:
        with recorder.stage("model_v5_shadow", PIPELINE_STAGE_SEQUENCE["model_v5_shadow"]) as st:
            st.result = results["model_v5_shadow"] = run_v5_shadow(today)
    except Exception:
        logger.exception("Model v5 shadow run failed")

    for stage_name in ("investment_intelligence", "market_opportunity", "macro_exposure"):
        stage_result = results.get(stage_name) or {}
        failed = int(stage_result.get("failed", 0))
        targets = int(stage_result.get("targets", 0))
        attempted = int(stage_result.get("succeeded", 0)) + int(stage_result.get("no_finding", 0)) + failed + int(stage_result.get("with_data", 0))
        if failed:
            health.append(HealthFinding(code="live_intelligence_collection_failed", severity="warning",
                message=f"{stage_name} recorded {failed} failed collection attempts", detail={"stage": stage_name, "failed": failed, "targets": targets}))
        if targets and attempted == 0:
            health.append(HealthFinding(code="live_intelligence_silent_failure", severity="warning",
                message=f"{stage_name} had {targets} targets but recorded no attempts", detail={"stage": stage_name, "targets": targets}))

    # 隔離状態はcollection工程でのみ変わる(is_quarantinedを更新するのは
    # collect_oneのみ。以降のgates/scoring/forward_validation/filingsは読むだけ)。
    # 上のcollection工程で読んだ値をそのまま使い、同じ集計を2度読まない。
    health.extend(check_quarantine_health(quarantined_count, universe_size))  # 18.7

    # 30.7.4:日次パイプラインの最後(バックアップの前)に差し込む。失敗しても
    # 止めない——保有・追跡銘柄のアラート生成はオプション機能であり、
    # 当日のスコア計算・収集の成果を無駄にする理由にはならない(18.4と同じ扱い)。
    logger.info("running quarterly monitoring")
    try:
        with recorder.stage("monitoring", PIPELINE_STAGE_SEQUENCE["monitoring"]) as st:
            st.result = results["monitoring"] = run_monitoring(today)
    except Exception:
        logger.exception("run_monitoring failed")

    logger.info("running backup")
    try:
        with recorder.stage("backup", PIPELINE_STAGE_SEQUENCE["backup"]) as st:
            backup_path = run_backup()
            st.result = {"path": str(backup_path)}
    except Exception:
        # バックアップ失敗はパイプライン全体を失敗にはしない(18.4:検証資産の
        # 保護は重要だが、当日のスコア計算自体を無駄にする理由にはならない)。
        # ログには残し、次回実行時のリトライに委ねる。
        logger.exception("backup failed")

    # §4.4:履歴を無限に伸ばさない。backup工程の直後(=全工程完了後)に刈り込む。
    recorder.prune_old_runs()

    # §3.4:既存3閾値では拾えない「例外なく完走したのに成果が実質ゼロ」の検出。
    # 全工程完了後でなければ判定できない(non_core_failed_stagesは全工程の
    # 結果が出そろって初めて確定し、scoring_yield_droppedは前回実行との比較を
    # 要る)。
    health.extend(
        check_pipeline_health(
            target_count=len(symbols),
            universe_size=universe_size,
            scoring_result=results.get("scoring"),
            previous_scored=recorder.previous_scored(),
            failed_stages=recorder.non_core_failed_stages(),
        )
    )
    recorder.finish(health)

    return results
