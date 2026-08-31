"""運用アラート閾値の判定(18.7)。

個人利用規模(11.1解釈A)のため、通知はログのWARNING/ERRORレベル出力に
留める(`scripts/run_daily_pipeline.bat` がログファイルに書き出すため、
異常時はそこで気づける)。メール・デスクトップ通知等への拡張は将来課題。

**2026-08-30(docs/daily_job_status_screen_2026-08-30.md、14.15の運用監視):**
判定結果を `list[HealthFinding]` として構造化して返すよう拡張した。
ログ出力(18.7)はそのまま維持し、戻り値を追加しただけ——既存3閾値の値・
判定ロジックは1行も変えていない(実データに基づく根拠は各定数のコメントを
参照。特にE-2のsanitized比率18.7%の実測値)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

COLLECTION_SUCCESS_WARN_THRESHOLD = 0.95
COLLECTION_SUCCESS_ERROR_THRESHOLD = 0.90
QUARANTINE_WARN_RATIO = 0.05
QUARANTINE_ERROR_RATIO = 0.10

# E-2(2026-08-27、docs/defect_audit_2026-08-27.md):`sanitized`(旧 `invalid_data`、
# B-7で改名)は「一部フィールドを無効化した上でスコアリングに正常採用された
# データ」であり収集の失敗ではない。実データでは全体の約18.7%を占めるため、
# 分子に含めないと平常運転日でも success_rate が81%前後に張り付き、ERROR閾値
# (0.90)を毎日下回ってアラート疲れを起こす。よって `success` と同列に数える。
# 一方で `sanitized` の比率が高すぎる状態はデータ品質劣化のシグナルなので、
# 別のWARNINGとして独立に監視する。
SANITIZED_RATIO_WARN_THRESHOLD = 0.30

# 新規(2026-08-30、docs/daily_job_status_screen_2026-08-30.md §3.4)。既存3閾値と
# 異なり実データの根拠は無い——「前回の半分を下回ったら警告」という粗い相対
# 基準で、08-29型の静かな劣化(前回5,287件→今日0件、のような)を検出する
# ためのセーフティネットにすぎない。根拠を持って調整されるまでは変更しない。
SCORING_YIELD_DROP_RATIO = 0.5

# §3.3:このいずれかが failed なら run 全体を failed とみなす。daily_pipeline.py
# で例外を握り潰さず全体停止させる工程(=停止則を持つ工程)と一致させている。
CORE_STAGES = frozenset({"collection", "gates", "scoring", "forward_validation"})


@dataclass(frozen=True)
class HealthFinding:
    """健全性所見1件(§3.4)。

    `code` はフロントの日本語説明(`frontend/src/pipelineHealth.ts`)と対応させる
    キー。`message` は既存3判定については既存のログ文言をそのまま使う——UIでの
    表示はフロント側が `code` ごとに別途持つ日本語文言を使う(§6.2。既存の
    ログ文言はトラブルシュート用の英語ログ体裁のままであり、そのままUIに出すと
    利用者に読めない)。
    """

    code: str
    severity: str  # "warning" / "error"
    message: str
    detail: dict


def check_collection_health(status_counts: dict[str, int]) -> list[HealthFinding]:
    total = sum(status_counts.values())
    if total == 0:
        return []
    findings: list[HealthFinding] = []

    # E-2: `sanitized` は失敗ではなく正常採用データなので成功の分子に含める。
    success = status_counts.get("success", 0) + status_counts.get("sanitized", 0)
    success_rate = success / total
    if success_rate < COLLECTION_SUCCESS_ERROR_THRESHOLD:
        message = f"collection success rate critically low: {success_rate * 100:.1f}% ({success}/{total}) — see 18.7"
        logger.error(message)
        findings.append(
            HealthFinding(
                code="collection_success_rate_low",
                severity="error",
                message=message,
                detail={"success_rate": success_rate, "success": success, "total": total},
            )
        )
    elif success_rate < COLLECTION_SUCCESS_WARN_THRESHOLD:
        message = f"collection success rate degraded: {success_rate * 100:.1f}% ({success}/{total}) — see 18.7"
        logger.warning(message)
        findings.append(
            HealthFinding(
                code="collection_success_rate_low",
                severity="warning",
                message=message,
                detail={"success_rate": success_rate, "success": success, "total": total},
            )
        )

    # E-2: sanitized 比率自体の劣化監視(閾値は暫定値。実データの collection_logs
    # 分布を見て根拠を持って調整すること。平常時の実測は約18.7%)。
    sanitized = status_counts.get("sanitized", 0)
    sanitized_ratio = sanitized / total
    if sanitized_ratio > SANITIZED_RATIO_WARN_THRESHOLD:
        message = (
            f"sanitized data ratio elevated: {sanitized_ratio * 100:.1f}% ({sanitized}/{total}) "
            "— data quality may be degrading (see E-2)"
        )
        logger.warning(message)
        findings.append(
            HealthFinding(
                code="sanitized_ratio_elevated",
                severity="warning",
                message=message,
                detail={"sanitized_ratio": sanitized_ratio, "sanitized": sanitized, "total": total},
            )
        )
    return findings


def check_quarantine_health(quarantined_count: int, universe_size: int) -> list[HealthFinding]:
    if universe_size == 0:
        return []
    findings: list[HealthFinding] = []
    ratio = quarantined_count / universe_size
    if ratio > QUARANTINE_ERROR_RATIO:
        message = (
            f"quarantine ratio critically high: {ratio * 100:.1f}% ({quarantined_count}/{universe_size}) — see 18.7"
        )
        logger.error(message)
        findings.append(
            HealthFinding(
                code="quarantine_ratio_high",
                severity="error",
                message=message,
                detail={"ratio": ratio, "quarantined": quarantined_count, "universe_size": universe_size},
            )
        )
    elif ratio > QUARANTINE_WARN_RATIO:
        message = f"quarantine ratio elevated: {ratio * 100:.1f}% ({quarantined_count}/{universe_size}) — see 18.7"
        logger.warning(message)
        findings.append(
            HealthFinding(
                code="quarantine_ratio_high",
                severity="warning",
                message=message,
                detail={"ratio": ratio, "quarantined": quarantined_count, "universe_size": universe_size},
            )
        )
    return findings


def check_pipeline_health(
    *,
    target_count: int,
    universe_size: int,
    scoring_result: dict[str, int] | None,
    previous_scored: int | None,
    failed_stages: list[str],
) -> list[HealthFinding]:
    """既存3閾値では拾えない「例外なく完走したのに成果が実質ゼロ」を検出する
    (§3.4新規、docs/daily_job_status_screen_2026-08-30.md)。2026-08-29の実運用は
    ここで足す3判定すべてに引っかかる(それがこの画面の存在理由)。

    `failed_stages` は**中核工程を除いた**失敗工程名のリストを呼び出し側
    (`PipelineRecorder.non_core_failed_stages()`)が渡す——中核工程の失敗は
    run全体を `failed` にする側で扱うため、ここでは重ねて所見にしない。
    """
    findings: list[HealthFinding] = []

    # 08-29の主症状:全銘柄隔離で収集対象そのものが0件になると、収集は
    # 「例外なく完了」する。check_collection_health は処理件数の合計を見るため
    # total=0 で早期returnし、この状態を素通りする。対象選定の時点
    # (`select_collectable_symbols` の結果件数)を別に見て検出する。
    if target_count == 0 and universe_size > 0:
        message = f"収集対象が0件でした(ユニバース{universe_size}銘柄すべてが隔離中の可能性があります)"
        logger.error("collection target is empty: 0 of %d universe tickers selected — see 18.7", universe_size)
        findings.append(
            HealthFinding(
                code="collection_target_empty",
                severity="error",
                message=message,
                detail={"universe_size": universe_size, "target_count": target_count},
            )
        )

    skipped_reason = (scoring_result or {}).get("skipped_reason")
    if skipped_reason:
        # run_scoring自身がerrorログ済みの中断理由をそのまま利用者向けに出す。
        # ランキングが更新されていないという、利用者に最も直接影響する所見。
        message = f"スコアリングが中断しました:{skipped_reason}"
        logger.error("scoring skipped: %s — see 18.7", skipped_reason)
        findings.append(
            HealthFinding(
                code="scoring_skipped",
                severity="error",
                message=message,
                detail={"skipped_reason": skipped_reason},
            )
        )
    elif scoring_result is not None and previous_scored:
        # 前回0件だった場合・初回実行(previous_scored is None)では判定しない
        # (基準が無い比較は意味を持たない)。
        scored = scoring_result.get("scored", 0)
        if scored < previous_scored * SCORING_YIELD_DROP_RATIO:
            message = f"スコア付与数が前回実行から大きく減少しました({previous_scored}件 → {scored}件)"
            logger.warning("scoring yield dropped: %d (previous %d) — see 18.7", scored, previous_scored)
            findings.append(
                HealthFinding(
                    code="scoring_yield_dropped",
                    severity="warning",
                    message=message,
                    detail={"scored": scored, "previous_scored": previous_scored},
                )
            )

    for stage in failed_stages:
        message = f"工程「{stage}」が失敗しました"
        logger.warning("non-core stage failed: %s — see 18.7", stage)
        findings.append(
            HealthFinding(code="stage_failed", severity="warning", message=message, detail={"stage": stage})
        )

    return findings


def determine_run_status(stage_statuses: dict[str, str], health: list[HealthFinding]) -> str:
    """`pipeline_runs.status` を決める(§3.3)。

    孤児実行(プロセスが死んで `finished_at` が残らない実行)の判定はここでは
    行わない——死亡を検知する主体がバッチ内には存在しないため、DBの状態を
    「修復」すると嘘になりうる。孤児判定はAPI層のみで行う(§4.3)。
    """
    if any(stage_statuses.get(stage) == "failed" for stage in CORE_STAGES):
        return "failed"
    if health or any(status == "failed" for status in stage_statuses.values()):
        return "degraded"
    return "succeeded"
