import logging

from autoscreener.monitoring import HealthFinding, check_collection_health, check_quarantine_health


def test_healthy_collection_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        result = check_collection_health({"success": 97, "transient_failure": 3})
    assert caplog.records == []
    assert result == []


def test_degraded_collection_logs_warning(caplog):
    with caplog.at_level(logging.WARNING):
        result = check_collection_health({"success": 92, "transient_failure": 8})
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    # 2026-08-30(daily_job_status_screen_2026-08-30.md §3.4):戻り値を
    # list[HealthFinding] に構造化した。ログ出力自体は変えていない
    # (閾値・判定ロジックも1行も変えていない)。
    assert result == [
        HealthFinding(
            code="collection_success_rate_low",
            severity="warning",
            message=caplog.records[0].message,
            detail={"success_rate": 0.92, "success": 92, "total": 100},
        )
    ]


def test_critical_collection_logs_error(caplog):
    with caplog.at_level(logging.WARNING):
        result = check_collection_health({"success": 80, "transient_failure": 20})
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "ERROR"
    assert len(result) == 1
    assert result[0].code == "collection_success_rate_low"
    assert result[0].severity == "error"
    assert result[0].detail == {"success_rate": 0.80, "success": 80, "total": 100}


def test_empty_status_counts_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        result = check_collection_health({})
    assert caplog.records == []
    assert result == []


def test_sanitized_status_counts_toward_success_rate(caplog):
    """E-2: sanitizedは失敗ではなく正常採用データなので、成功率の分子に含めること。
    実データでの発生率(約18.7%)相当の入力でERROR/WARNINGが発火しないことを確認する。"""
    status_counts = {"success": 700, "sanitized": 200, "permanent_failure": 20}
    # (700 + 200) / 920 ≈ 97.8% となり、WARN閾値(0.95)もERROR閾値(0.90)も上回る。
    with caplog.at_level(logging.WARNING):
        result = check_collection_health(status_counts)
    assert caplog.records == []
    assert result == []


def test_elevated_sanitized_ratio_logs_warning(caplog):
    """E-2: sanitized比率が閾値を超えたら、成功率とは独立にWARNINGを出すこと。"""
    with caplog.at_level(logging.WARNING):
        result = check_collection_health({"success": 500, "sanitized": 500})
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert "sanitized data ratio elevated" in caplog.records[0].message
    assert len(result) == 1
    assert result[0].code == "sanitized_ratio_elevated"
    assert result[0].severity == "warning"
    assert "sanitized data ratio elevated" in result[0].message


def test_healthy_quarantine_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        result = check_quarantine_health(quarantined_count=10, universe_size=1000)
    assert caplog.records == []
    assert result == []


def test_elevated_quarantine_logs_warning(caplog):
    with caplog.at_level(logging.WARNING):
        result = check_quarantine_health(quarantined_count=60, universe_size=1000)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert len(result) == 1
    assert result[0] == HealthFinding(
        code="quarantine_ratio_high",
        severity="warning",
        message=caplog.records[0].message,
        detail={"ratio": 0.06, "quarantined": 60, "universe_size": 1000},
    )


def test_critical_quarantine_logs_error(caplog):
    with caplog.at_level(logging.WARNING):
        result = check_quarantine_health(quarantined_count=150, universe_size=1000)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "ERROR"
    assert len(result) == 1
    assert result[0].code == "quarantine_ratio_high"
    assert result[0].severity == "error"


def test_empty_universe_logs_nothing():
    result = check_quarantine_health(quarantined_count=0, universe_size=0)
    assert result == []
