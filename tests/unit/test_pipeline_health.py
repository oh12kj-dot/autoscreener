"""tests/unit/test_pipeline_health.py

daily_job_status_screen_2026-08-30.md §7。

2026-08-29の実運用(全工程succeeded・収集対象0件・スコアリング中断・
隔離率100%)が`degraded`として検出されることを確認する
——**このテストがこの実装の存在理由である**(§7)。
"""

from __future__ import annotations

from autoscreener.monitoring import (
    check_collection_health,
    check_pipeline_health,
    check_quarantine_health,
    determine_run_status,
)

_ALL_STAGES = [
    "universe_refresh",
    "cik_map_refresh",
    "macro",
    "xbrl_facts",
    "events",
    "insider",
    "short_interest",
    "collection",
    "gates",
    "backtest",
    "scoring",
    "forward_validation",
    "filings",
    "monitoring",
    "backup",
]


def test_2026_08_29_reproduction_is_degraded_with_three_findings():
    """全工程succeeded・収集0件・スコアリング中断・隔離率100%(5312/5312)。"""
    stage_statuses = {name: "succeeded" for name in _ALL_STAGES}
    collection_result: dict[str, int] = {}  # 対象0件なので処理件数の合計も0
    scoring_result = {
        "scored": 0,
        "unmeasurable": 0,
        "skipped_reason": "insufficient_price_coverage (0.1% of 1260 gated tickers "
        "have a 2026-08-26 price row, need 90%)",
    }

    health = []
    health += check_collection_health(collection_result)
    health += check_quarantine_health(5312, 5312)
    health += check_pipeline_health(
        target_count=0,
        universe_size=5312,
        scoring_result=scoring_result,
        previous_scored=1204,
        failed_stages=[],
    )

    codes = [f.code for f in health]
    assert set(codes) == {"collection_target_empty", "scoring_skipped", "quarantine_ratio_high"}
    assert len(health) == 3
    assert all(f.severity == "error" for f in health)

    assert determine_run_status(stage_statuses, health) == "degraded"


def test_scoring_yield_dropped_warns_on_large_drop():
    health = check_pipeline_health(
        target_count=100,
        universe_size=200,
        scoring_result={"scored": 500},
        previous_scored=1204,
        failed_stages=[],
    )
    assert [f.code for f in health] == ["scoring_yield_dropped"]
    assert health[0].severity == "warning"
    assert health[0].detail == {"scored": 500, "previous_scored": 1204}


def test_scoring_yield_dropped_not_judged_when_previous_scored_is_zero():
    """前回0件では判定しない(基準が無い比較は意味を持たない)。"""
    health = check_pipeline_health(
        target_count=100,
        universe_size=200,
        scoring_result={"scored": 0},
        previous_scored=0,
        failed_stages=[],
    )
    assert health == []


def test_scoring_yield_dropped_not_judged_without_previous_run():
    """前回実行が無い(初回実行)場合も例外にならず、判定しない。"""
    health = check_pipeline_health(
        target_count=100,
        universe_size=200,
        scoring_result={"scored": 0},
        previous_scored=None,
        failed_stages=[],
    )
    assert health == []


def test_scoring_yield_dropped_not_triggered_when_stable():
    health = check_pipeline_health(
        target_count=100,
        universe_size=200,
        scoring_result={"scored": 1180},
        previous_scored=1204,
        failed_stages=[],
    )
    assert health == []


def test_collection_target_empty_not_raised_when_universe_also_empty():
    """ユニバースが一度も構築されていない(初回セットアップ等)なら誤検知しない。"""
    health = check_pipeline_health(
        target_count=0,
        universe_size=0,
        scoring_result=None,
        previous_scored=None,
        failed_stages=[],
    )
    assert health == []


def test_stage_failed_finding_emitted_per_non_core_stage():
    health = check_pipeline_health(
        target_count=10,
        universe_size=20,
        scoring_result={"scored": 5},
        previous_scored=None,
        failed_stages=["filings", "backup"],
    )
    assert [f.code for f in health] == ["stage_failed", "stage_failed"]
    assert all(f.severity == "warning" for f in health)
    assert {f.detail["stage"] for f in health} == {"filings", "backup"}


def test_core_stage_failed_makes_run_failed():
    stage_statuses = {"collection": "failed", "gates": "succeeded", "scoring": "succeeded"}
    assert determine_run_status(stage_statuses, []) == "failed"


def test_non_core_stage_failed_makes_run_degraded_not_failed():
    stage_statuses = {"collection": "succeeded", "gates": "succeeded", "filings": "failed"}
    assert determine_run_status(stage_statuses, []) == "degraded"


def test_health_findings_alone_make_run_degraded():
    stage_statuses = {name: "succeeded" for name in ["collection", "gates", "scoring", "forward_validation"]}
    health = check_pipeline_health(
        target_count=0, universe_size=100, scoring_result=None, previous_scored=None, failed_stages=[]
    )
    assert determine_run_status(stage_statuses, health) == "degraded"


def test_tuesday_all_weekly_skipped_is_succeeded_without_findings():
    """`skipped`は`failed`ではない。火曜に週次8工程がskippedでも、他が正常なら
    `succeeded`(§3.3、§9)。"""
    stage_statuses = {
        "universe_refresh": "skipped",
        "cik_map_refresh": "skipped",
        "macro": "skipped",
        "xbrl_facts": "skipped",
        "events": "skipped",
        "insider": "skipped",
        "short_interest": "skipped",
        "backtest": "skipped",
        "collection": "succeeded",
        "gates": "succeeded",
        "scoring": "succeeded",
        "forward_validation": "succeeded",
        "filings": "succeeded",
        "monitoring": "succeeded",
        "backup": "succeeded",
    }
    assert determine_run_status(stage_statuses, []) == "succeeded"
