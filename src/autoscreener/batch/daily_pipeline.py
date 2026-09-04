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

**2026-09-04(A-2、docs/racr_wp_a_operational_safety_2026-09-04.md、
監査§10.2/10.3):** `run_daily_pipeline()` 全体を outer try/except/finally
で包んだ。`collection`/`gates`/`scoring`/`forward_validation` は上の§9の
とおり意図的に個別のtry/exceptを持たない停止則の工程だが、その例外が
この関数自身から抜けると `recorder.finish()` に到達せず、
`pipeline_runs.status` が `running` のまま永久に残る——2026-09-03の
gate stage FK違反が実際にこれで発生した。**個々の工程の停止則は一切
変えていない**(collectionの中身で失敗を握り潰すようにはしていない)。
変えたのは「run全体としての確定処理を必ず1回だけ、どの経路で終わっても
実行する」という一段外側の保証だけである。
"""

from __future__ import annotations

import datetime
import logging
import uuid

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
from autoscreener.batch.market_session import assess_market_session
from autoscreener.batch.pipeline_recorder import PipelineRecorder, sweep_orphan_runs
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
from autoscreener.db.models import PipelineRun
from autoscreener.db.session import session_scope
from autoscreener.monitoring import HealthFinding, check_collection_health, check_pipeline_health, check_quarantine_health
from autoscreener.pipeline_stages import PIPELINE_STAGE_SEQUENCE
from autoscreener.scoring.engine import run_scoring
from autoscreener.scoring.v5.engine import run_v5_shadow
from autoscreener.scoring.forward_validation import run_forward_validation, run_forward_validation_v5

logger = logging.getLogger(__name__)


def _run_stage_unless_resumed(
    recorder: PipelineRecorder,
    results: dict[str, dict],
    previous_results: dict[str, dict],
    name: str,
    sequence: int,
    work,
) -> None:
    """A-6:`previous_results`(前回succeededした工程の結果)に `name` が
    既にあれば、実際の処理(`work`)を呼ばずその結果を再利用する。

    新規run(`previous_results == {}`)では常に `work()` を呼ぶので、
    このヘルパを経由しても非再開時の挙動は完全に同じになる。**このヘルパ
    自身は例外を握り潰さない**(`recorder.stage()` と同じ「記録するだけ」の
    立場。§9のstop則——どの工程が全体を止め、どの工程が握り潰すかは
    呼び出し側のtry/except配置がそのまま決める)。
    """
    if name in previous_results:
        logger.info("resume: reusing already-succeeded stage %r from a previous attempt", name)
        results[name] = previous_results[name]
        return
    with recorder.stage(name, sequence) as st:
        st.result = results[name] = work()


def _skip_stage_unless_resumed(
    recorder: PipelineRecorder,
    results: dict[str, dict],
    previous_results: dict[str, dict],
    name: str,
    reason: str,
) -> None:
    if name in previous_results:
        results[name] = previous_results[name]
        return
    recorder.skip(name, PIPELINE_STAGE_SEQUENCE[name], reason)
    results[name] = {"skipped": 1, "skipped_reason": reason}


def _find_resumable_run_id(today: datetime.date) -> uuid.UUID | None:
    """A-6(2026-09-04、docs/racr_wp_a_operational_safety_2026-09-04.md、
    監査§10.3):今日ぶんの、`succeeded` で終わっていない直近のrunを探す。

    `succeeded` を除外するのは「今日はもう完走している」場合に `--resume`
    を誤って新しい部分再実行にしないため。見つからなければ通常どおり新規
    runを作る(=`--resume` を付けても実害が無い。安全側のフォールバック)。
    """
    with session_scope() as session:
        row = (
            session.query(PipelineRun)
            .filter(PipelineRun.run_date == today, PipelineRun.status != "succeeded")
            .order_by(PipelineRun.started_at.desc())
            .first()
        )
    return row.run_id if row is not None else None


def run_daily_pipeline(*, resume: bool = False) -> dict[str, dict[str, int]]:
    """日次パイプラインを実行する。

    `resume=True`(A-6、CLIの `run-daily-pipeline --resume`)のときは、
    今日ぶんの未完走runがあればそれへ合流し、既に `succeeded` した工程を
    再実行しない——2時間かかるcollectionの後にgateで落ちても、その2時間を
    毎回捨てないためのもの(監査§10.3)。未完走runが見つからなければ
    通常どおり新規runになる。
    """
    # A-2:新しいrunを始める前に、前回以前の孤児run(heartbeatが90分以上
    # 止まったままの `running` run)を回収する。2026-09-03の停止runが
    # 手作業のUPDATEなしに次回実行で自然に `aborted` へ落ちるのはこの
    # 呼び出しによる(監査§10.2「手作業のUPDATEを前提にしない」)。
    swept = sweep_orphan_runs()
    if swept:
        logger.warning("swept %d orphaned pipeline run(s) before starting: %s", len(swept), swept)

    today = utc_today()
    is_weekly = today.weekday() == WEEKLY_REFRESH_WEEKDAY

    resume_run_id = _find_resumable_run_id(today) if resume else None
    if resume and resume_run_id is None:
        logger.info("--resume was requested but no incomplete run exists for %s; starting a fresh run", today)

    # 14.15:工程ごとの実行記録。トリガー種別(scheduled/manual)を区別する
    # 呼び出し経路が現状1つ(CLI)しか無いため、既定値のまま固定する。
    recorder = PipelineRecorder(today, is_weekly, resume_run_id=resume_run_id)
    # A-6:再開時は前回succeededした工程の結果を種にする。呼び出し側
    # (`_run_daily_pipeline_body`)はこれに含まれる工程名を実際には
    # 再実行しない。新規run(resume_run_id=None)では常に空。
    previous_results = recorder.resumed_stage_results()
    results: dict[str, dict[str, int]] = dict(previous_results)
    health: list[HealthFinding] = []

    try:
        _run_daily_pipeline_body(today, is_weekly, recorder, results, health, previous_results)
    except Exception as exc:
        # A-2:collection/gates/scoring/forward_validationは意図的に
        # try/exceptで囲んでいない(§9の停止則)。その例外がここまで
        # 抜けてきても、runを`running`のまま残さず`failed`で確定してから
        # 再送出する——CLI/スケジューラは従来どおり非0終了で失敗を検知できる。
        recorder.finish_with_exception(exc)
        raise
    return results


def _run_daily_pipeline_body(
    today: datetime.date,
    is_weekly: bool,
    recorder: PipelineRecorder,
    results: dict[str, dict[str, int]],
    health: list[HealthFinding],
    previous_results: dict[str, dict],
) -> None:
    """`run_daily_pipeline()` の本体(A-2で outer try/except/finally から
    分離)。**工程の並び・停止則・try/except構造は分離前と1バイトも
    変えていない**——変えたのは呼び出し元がこの関数を包む外殻と、A-6で
    各工程呼び出しを `_run_stage_unless_resumed()` 経由にしたことだけである。
    """
    if is_weekly:
        logger.info("weekly universe refresh (weekday=%s)", today.weekday())
        _run_stage_unless_resumed(
            recorder, results, previous_results,
            "universe_refresh", PIPELINE_STAGE_SEQUENCE["universe_refresh"],
            lambda: {"candidates": refresh_universe(today)},
        )

        # 30.3.2:CIK突合もユニバース再取得と同じ週次サイクルで回す(新規上場銘柄の
        # CIKを取り込むため)。EDGAR_USER_AGENT未設定の環境(30.3.1)ではEDGAR連携
        # 全体を使わない選択をしている利用者がいるため、失敗はログに残しつつ
        # パイプライン全体は止めない(backupと同じ扱い)。
        logger.info("weekly CIK map refresh")
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "cik_map_refresh", PIPELINE_STAGE_SEQUENCE["cik_map_refresh"], refresh_cik_map,
            )
        except Exception:
            logger.exception("weekly CIK map refresh failed (EDGAR_USER_AGENT not set?)")

        # 30.8.2:財務データと同じく、マクロ系列も日々変わるものではないので週次で足りる。
        logger.info("weekly macro collection")
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "macro", PIPELINE_STAGE_SEQUENCE["macro"], collect_macro,
            )
        except Exception:
            logger.exception("weekly macro collection failed (FRED_API_KEY not set?)")

        # 30.5.5:XBRL実績値も財務データなので四半期に1回しか変わらない。週次で足りる。
        logger.info("weekly XBRL facts collection")
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "xbrl_facts", PIPELINE_STAGE_SEQUENCE["xbrl_facts"], collect_xbrl_facts,
            )
        except Exception:
            logger.exception("weekly XBRL facts collection failed (EDGAR_USER_AGENT not set?)")

        # J-6(docs/investment_decision_gap_2026-08-29.md):次回決算日の収集。yfinance の
        # スナップショットしか取れずレート制限も食うので、追跡対象のみ・週次で足りる。
        # 失敗してもパイプラインは止めない。
        logger.info("weekly event calendar collection")
        try:
            from autoscreener.batch.collect_events import collect_events

            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "events", PIPELINE_STAGE_SEQUENCE["events"], collect_events,
            )
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

            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "insider", PIPELINE_STAGE_SEQUENCE["insider"], collect_insider,
            )
        except Exception:
            logger.exception("weekly insider transactions collection failed")

        logger.info("weekly short interest collection")
        try:
            from autoscreener.batch.collect_supply import collect_short_interest

            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "short_interest", PIPELINE_STAGE_SEQUENCE["short_interest"], collect_short_interest,
            )
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
        market_decision = assess_market_session(
            session,
            target_count=len(symbols),
            minimum_coverage=collection_config.market_session_min_coverage,
            symbols=symbols,
        )

    collection_symbols = (
        list(market_decision.symbols_to_collect)
        if market_decision.should_run and market_decision.symbols_to_collect
        else symbols
    )
    logger.info(
        "daily collection: %d/%d symbols need session %s",
        len(collection_symbols),
        len(symbols),
        market_decision.expected_session,
    )
    # A-6:collectionはこのpipelineで最も時間のかかる工程(数十分〜数時間、
    # 監査§10.3「2時間超のcollection」)であり、resumeの主眼はまさにこれを
    # 捨てないこと。`quarantined`/`universe_size` は `st.result` にのみ添える
    # (`results["collection"]`自体は汚さない、という既存方針を維持)ため、
    # 再開時は前回の値をそのまま引き継ぐ——`collection_population_counts`は
    # collection以降どの工程でも変わらない値を数えるだけなので、取り直しても
    # 再計算にすぎず、再実行不要というresumeの前提と矛盾しない。
    if "collection" in previous_results:
        logger.info("resume: reusing already-succeeded stage 'collection' from a previous attempt")
        results["collection"] = previous_results["collection"]
        quarantined_count = previous_results["collection"].get("quarantined", 0)
        universe_size = previous_results["collection"].get("universe_size", 0)
    elif market_decision.should_run:
        with recorder.stage("collection", PIPELINE_STAGE_SEQUENCE["collection"]) as st:
            results["collection"] = run_daily_collection(
                collection_symbols,
                collection_config=collection_config,
                snapshot_date=today,
                market_session_date=market_decision.expected_session,
            )
            # 18.1/18.7:隔離状態は収集工程(`collect_one`)でのみ更新され、以降の
            # 工程では変わらない。ここで1度だけ読み、隔離健全性判定と画面の
            # 「隔離率」タイル(§6.3)の両方に使う。`results["collection"]`(戻り値・
            # CLI出力に使われる)自体は汚さず、記録用の `st.result` にだけ添える。
            with session_scope() as session:
                quarantined_count, universe_size = collection_population_counts(session)
            st.result = {**results["collection"], "quarantined": quarantined_count, "universe_size": universe_size}
    else:
        logger.info(
            "market stages skipped: expected_session=%s latest_covered=%s coverage=%d/%d",
            market_decision.expected_session,
            market_decision.latest_covered_session,
            market_decision.covered_count,
            market_decision.target_count,
        )
        _skip_stage_unless_resumed(
            recorder, results, previous_results, "collection", market_decision.reason or "no_new_market_session"
        )
        with session_scope() as session:
            quarantined_count, universe_size = collection_population_counts(session)
    # A resumed run may see the just-written market session as fully covered.
    # In that case the collection stage must be reused, while its unfinished
    # downstream stages still need to run.
    market_work_available = market_decision.should_run or "collection" in previous_results
    if market_work_available:
        health.extend(check_collection_health(results["collection"]))  # 18.7

    # TENX v2: append-only current consensus. This is a display/PIT-history
    # layer and failures must not prevent the deterministic core score.
    logger.info("collecting analyst consensus snapshots")
    if market_work_available:
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "consensus", PIPELINE_STAGE_SEQUENCE["consensus"], collect_consensus,
            )
        except Exception:
            logger.exception("consensus collection failed")
    else:
        _skip_stage_unless_resumed(
            recorder, results, previous_results, "consensus", "no_new_market_session"
        )

    logger.info("applying gates")
    if market_work_available:
        _run_stage_unless_resumed(
            recorder, results, previous_results,
            "gates", PIPELINE_STAGE_SEQUENCE["gates"], lambda: apply_gates(today),
        )
    else:
        _skip_stage_unless_resumed(recorder, results, previous_results, "gates", "no_new_market_session")

    if is_weekly:
        # 28.8:較正写像を最新の観測で学習し直す。**スコアリングより前**に
        # 実行するのが要点——順序が逆だと、その週のスコアは1週間古い較正で
        # 書かれる。失敗してもパイプライン全体は止めない(較正が無ければ
        # スコアは未較正のまま保存され、UIがその状態を明示する)。
        logger.info("weekly backtest (recalibration)")
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "backtest", PIPELINE_STAGE_SEQUENCE["backtest"],
                lambda: {"observations": run_backtest().observation_count},
            )
        except Exception:
            logger.exception("weekly backtest failed — scores will use the previous calibration map")
    else:
        recorder.skip("backtest", PIPELINE_STAGE_SEQUENCE["backtest"], "not_weekly")

    if market_work_available:
        logger.info("running scoring")
        _run_stage_unless_resumed(
            recorder, results, previous_results,
            "scoring", PIPELINE_STAGE_SEQUENCE["scoring"], lambda: run_scoring(today),
        )

        logger.info("running forward validation")
        _run_stage_unless_resumed(
            recorder, results, previous_results,
            "forward_validation", PIPELINE_STAGE_SEQUENCE["forward_validation"], lambda: run_forward_validation(today),
        )
    else:
        _skip_stage_unless_resumed(recorder, results, previous_results, "scoring", "no_new_market_session")
        _skip_stage_unless_resumed(recorder, results, previous_results, "forward_validation", "no_new_market_session")

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
        _run_stage_unless_resumed(
            recorder, results, previous_results,
            "filings", PIPELINE_STAGE_SEQUENCE["filings"],
            lambda: collect_filings(
                symbols=tracked_symbols,
                full_refresh=is_weekly,
                use_daily_index=not is_weekly,
                as_of=today,
            ),
        )
    except Exception:
        logger.exception("collect_filings failed (EDGAR_USER_AGENT not set, or SEC unavailable?)")

    changed_symbols = list((results.get("filings") or {}).get("changed_symbols") or [])
    derived_symbols = tracked_symbols if is_weekly else changed_symbols

    logger.info("extracting source sections for %d changed/reconciliation tickers", len(derived_symbols))
    if derived_symbols:
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "filing_sections", PIPELINE_STAGE_SEQUENCE["filing_sections"],
                lambda: collect_filing_sections(symbols=derived_symbols),
            )
        except Exception:
            logger.exception("filing section extraction failed")
    else:
        _skip_stage_unless_resumed(
            recorder, results, previous_results, "filing_sections", "no_new_filings"
        )

    disclosure_jobs = (
        ("guidance", collect_guidance),
        ("customer_concentration", collect_concentration),
        ("dilution", collect_dilution),
    )
    for stage_name, job in disclosure_jobs:
        if derived_symbols:
            try:
                _run_stage_unless_resumed(
                    recorder, results, previous_results,
                    stage_name, PIPELINE_STAGE_SEQUENCE[stage_name], lambda job=job: job(symbols=derived_symbols),
                )
            except Exception:
                logger.exception("%s extraction failed", stage_name)
        else:
            _skip_stage_unless_resumed(
                recorder, results, previous_results, stage_name, "no_new_filings"
            )

    # Litigation uses a batched CIK query and its own overlap cursor, so it must
    # see the complete tracked set rather than only issuers with new submissions.
    try:
        _run_stage_unless_resumed(
            recorder, results, previous_results,
            "litigation", PIPELINE_STAGE_SEQUENCE["litigation"],
            lambda: collect_litigation(symbols=tracked_symbols),
        )
    except Exception:
        logger.exception("litigation extraction failed")

    logger.info("extracting investment intelligence from stored filing sections")
    if derived_symbols:
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "investment_intelligence", PIPELINE_STAGE_SEQUENCE["investment_intelligence"],
                lambda: collect_investment_intelligence(symbols=derived_symbols),
            )
        except Exception:
            logger.exception("investment intelligence extraction failed")
    else:
        _skip_stage_unless_resumed(
            recorder, results, previous_results, "investment_intelligence", "no_new_filings"
        )

    if derived_symbols:
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "market_opportunity", PIPELINE_STAGE_SEQUENCE["market_opportunity"],
                lambda: collect_market_opportunity(symbols=derived_symbols),
            )
        except Exception:
            logger.exception("market opportunity collection failed")
    else:
        _skip_stage_unless_resumed(
            recorder, results, previous_results, "market_opportunity", "no_new_filings"
        )

    if market_work_available:
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "macro_exposure", PIPELINE_STAGE_SEQUENCE["macro_exposure"],
                lambda: collect_macro_exposure(symbols=tracked_symbols),
            )
        except Exception:
            logger.exception("macro exposure calculation failed")
    else:
        _skip_stage_unless_resumed(
            recorder, results, previous_results, "macro_exposure", "no_new_market_session"
        )

    # Issue #3 Phase 1: v5 is append-only and shadow-only.  A failure is
    # recorded as a non-core failed stage but never prevents v4 scoring or the
    # remaining operational stages from completing.
    if market_work_available:
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "model_v5_shadow", PIPELINE_STAGE_SEQUENCE["model_v5_shadow"], lambda: run_v5_shadow(today),
            )
        except Exception:
            logger.exception("Model v5 shadow run failed")
    else:
        _skip_stage_unless_resumed(
            recorder, results, previous_results, "model_v5_shadow", "no_new_market_session"
        )

    # A-4(2026-09-04、docs/racr_wp_a_operational_safety_2026-09-04.md、
    # 監査§10.1「`forward_validation_v5` は予約済みの26番だがdaily
    # pipelineへ配線されていない」・§0.8「V5実現forward return 0件」):
    # `run_forward_validation_v5()` とCLI(`run-forward-validation-v5`)は
    # 実装済み・real-DB testedで、配線だけが未了だった
    # (pipeline_stages.py の旧 `RESERVED_STAGE_NUMBERS`)。v4の
    # `forward_validation` と役割は同じだが、v5はまだshadowモデルであり
    # v4スコアリング・後続の運用工程を止める理由にはならないため、v4とは
    # 異なりnon-core(try/exceptで囲む)扱いにする——model_v5_shadowの後、
    # monitoringの前に実行する。
    if market_work_available:
        try:
            _run_stage_unless_resumed(
                recorder, results, previous_results,
                "forward_validation_v5", PIPELINE_STAGE_SEQUENCE["forward_validation_v5"],
                lambda: run_forward_validation_v5(today),
            )
        except Exception:
            logger.exception("Model v5 forward validation failed")
    else:
        _skip_stage_unless_resumed(
            recorder, results, previous_results, "forward_validation_v5", "no_new_market_session"
        )

    for stage_name in ("investment_intelligence", "market_opportunity", "macro_exposure"):
        stage_result = results.get(stage_name) or {}
        failed = int(stage_result.get("failed", 0))
        targets = int(stage_result.get("targets", 0))
        attempted = (
            int(stage_result.get("succeeded", 0))
            + int(stage_result.get("no_finding", 0))
            + int(stage_result.get("already_processed", 0))
            + failed
            + int(stage_result.get("with_data", 0))
        )
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
        _run_stage_unless_resumed(
            recorder, results, previous_results,
            "monitoring", PIPELINE_STAGE_SEQUENCE["monitoring"], lambda: run_monitoring(today),
        )
    except Exception:
        logger.exception("run_monitoring failed")

    logger.info("running backup")
    try:
        _run_stage_unless_resumed(
            recorder, results, previous_results,
            "backup", PIPELINE_STAGE_SEQUENCE["backup"], lambda: {"path": str(run_backup())},
        )
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
            # An intentional no-session skip is healthy.  Passing the explicit
            # skip result here would be misclassified as a silent scoring loss.
            scoring_result=results.get("scoring") if market_work_available else None,
            previous_scored=recorder.previous_scored(),
            failed_stages=recorder.non_core_failed_stages(),
        )
    )
    recorder.finish(health)
