"""`ops/doctor.py` の純粋判定ロジックのテスト(K-9)。

**DBにもネットワークにも触らない。** `run_doctor()` 自身(DB接続・alembic参照・
`.env` 読み込みを行うオーケストレーション層)はテストせず、素の値(行数・最終日付・
環境変数の有無)を引数に取る純関数だけを検証する。
"""

from __future__ import annotations

import datetime

from autoscreener.ops.doctor import (
    DoctorFinding,
    check_alembic_revision,
    check_db_connection,
    check_env_keys,
    check_last_pipeline_run,
    check_table_freshness,
    check_table_row_counts,
    format_doctor_report,
    wrap_quarantine_findings,
    DoctorReport,
)
from autoscreener.monitoring import HealthFinding


# --- check_env_keys ----------------------------------------------------------


def test_missing_edgar_user_agent_names_affected_tables_in_message():
    values = {"DATABASE_URL": "x", "API_DATABASE_URL": "x", "EDGAR_USER_AGENT": None, "FRED_API_KEY": "x"}
    findings = check_env_keys(values)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "env_key_missing"
    assert finding.severity == "error"
    for table in ("filings", "xbrl_facts", "delisted_at", "insider_transactions"):
        assert table in finding.message


def test_missing_fred_api_key_names_macro_series():
    values = {"DATABASE_URL": "x", "API_DATABASE_URL": "x", "EDGAR_USER_AGENT": "x", "FRED_API_KEY": ""}
    findings = check_env_keys(values)
    assert len(findings) == 1
    assert "macro_series" in findings[0].message


def test_all_keys_present_yields_no_findings():
    values = {"DATABASE_URL": "x", "API_DATABASE_URL": "x", "EDGAR_USER_AGENT": "x", "FRED_API_KEY": "x"}
    assert check_env_keys(values) == []


def test_secret_values_never_appear_in_findings():
    """環境変数の値が所見の文字列に混入しないこと(秘密の漏洩防止)。"""
    secret_db_url = "postgresql+psycopg://admin:s3cr3t-p4ssw0rd@prod-db.internal:5432/autoscreener"
    secret_edgar_ua = "TENX research <super-secret-address@example.com>"
    secret_fred_key = "ABCDEF0123456789SECRETFREDKEY"
    values = {
        "DATABASE_URL": secret_db_url,
        "API_DATABASE_URL": None,  # missing, to force a finding to be emitted
        "EDGAR_USER_AGENT": secret_edgar_ua,
        "FRED_API_KEY": secret_fred_key,
    }
    findings = check_env_keys(values)
    assert len(findings) == 1  # only API_DATABASE_URL missing
    rendered = " ".join(
        [findings[0].code, findings[0].severity, findings[0].message, findings[0].remedy, str(findings[0].detail)]
    )
    for secret in (secret_db_url, secret_edgar_ua, secret_fred_key, "s3cr3t-p4ssw0rd"):
        assert secret not in rendered


def test_db_connection_error_message_excludes_exception_string():
    """DSNを含みうる例外メッセージ自体は使わず、例外クラス名だけを出すこと。"""

    class FakeConnError(Exception):
        pass

    error = FakeConnError("connection to server at postgresql://user:hunter2@host failed")
    findings = check_db_connection(error)
    assert len(findings) == 1
    assert "hunter2" not in findings[0].message
    assert "hunter2" not in str(findings[0].detail)
    assert findings[0].severity == "error"


def test_db_connection_ok_yields_no_findings():
    assert check_db_connection(None) == []


# --- check_alembic_revision ---------------------------------------------------


def test_alembic_at_head_yields_no_findings():
    assert check_alembic_revision("abc123", "abc123") == []


def test_alembic_behind_head_is_error():
    findings = check_alembic_revision("abc123", "def456")
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].code == "alembic_not_at_head"


# --- check_table_row_counts ----------------------------------------------------


def _full_row_counts(**overrides: int) -> dict[str, int]:
    base = {
        "tickers": 5312,
        "raw_snapshots": 5000,
        "price_snapshots": 5000,
        "scores": 4800,
        "universe_snapshots": 5312,
        "filings": 10,
        "xbrl_facts": 10,
        "macro_series": 100,
        "event_calendar": 0,
        "insider_transactions": 0,
        "short_interest": 0,
        "forward_returns": 0,
        "pipeline_runs": 30,
    }
    base.update(overrides)
    return base


def test_forward_returns_zero_is_not_an_error():
    findings = check_table_row_counts(_full_row_counts(), edgar_enabled=True, fred_enabled=True)
    codes_for_forward_returns = [f for f in findings if f.detail.get("table") == "forward_returns"]
    assert codes_for_forward_returns == []


def test_scores_zero_is_an_error():
    findings = check_table_row_counts(_full_row_counts(scores=0), edgar_enabled=True, fred_enabled=True)
    matching = [f for f in findings if f.detail.get("table") == "scores"]
    assert len(matching) == 1
    assert matching[0].severity == "error"
    assert matching[0].code == "table_empty"


def test_pipeline_never_run_short_circuits_other_core_checks():
    """一度も実行されていない場合、他の中核テーブルの0件を重ねて報告しない。"""
    findings = check_table_row_counts(_full_row_counts(pipeline_runs=0, scores=0), edgar_enabled=True, fred_enabled=True)
    assert len(findings) == 1
    assert findings[0].code == "pipeline_never_run"
    assert findings[0].severity == "error"


def test_filings_zero_is_normal_when_edgar_disabled():
    findings = check_table_row_counts(_full_row_counts(filings=0, xbrl_facts=0), edgar_enabled=False, fred_enabled=True)
    assert [f for f in findings if f.detail.get("table") in ("filings", "xbrl_facts")] == []


def test_filings_zero_is_error_when_edgar_enabled():
    findings = check_table_row_counts(_full_row_counts(filings=0), edgar_enabled=True, fred_enabled=True)
    matching = [f for f in findings if f.detail.get("table") == "filings"]
    assert len(matching) == 1
    assert matching[0].code == "table_empty_despite_enabled"
    assert matching[0].severity == "error"


def test_macro_series_zero_is_normal_when_fred_disabled():
    findings = check_table_row_counts(_full_row_counts(macro_series=0), edgar_enabled=True, fred_enabled=False)
    assert [f for f in findings if f.detail.get("table") == "macro_series"] == []


def test_insider_and_short_interest_zero_never_errors():
    """collect_supply.pyの既定fetcherは空を返す設計なので、EDGAR有効時でも0は正常。"""
    findings = check_table_row_counts(_full_row_counts(), edgar_enabled=True, fred_enabled=True)
    assert [f for f in findings if f.detail.get("table") in ("insider_transactions", "short_interest")] == []


# --- check_table_freshness ------------------------------------------------------


def _full_latest_dates(today: datetime.date, **overrides: datetime.date | None) -> dict[str, datetime.date | None]:
    base: dict[str, datetime.date | None] = {
        "raw_snapshots": today,
        "price_snapshots": today,
        "scores": today,
        "universe_snapshots": today,
        "macro_series": today,
        "pipeline_runs": today,
    }
    base.update(overrides)
    return base


def test_freshness_within_threshold_is_silent():
    today = datetime.date(2026, 8, 30)
    latest_dates = _full_latest_dates(today, scores=today - datetime.timedelta(days=4))
    findings = check_table_freshness(latest_dates, today, edgar_enabled=True, fred_enabled=True)
    assert findings == []


def test_freshness_boundary_at_threshold_plus_one_is_stale():
    today = datetime.date(2026, 8, 30)
    latest_dates = _full_latest_dates(today, scores=today - datetime.timedelta(days=5))
    findings = check_table_freshness(latest_dates, today, edgar_enabled=True, fred_enabled=True)
    matching = [f for f in findings if f.detail.get("table") == "scores"]
    assert len(matching) == 1
    assert matching[0].severity == "warning"
    assert matching[0].code == "table_stale"


def test_freshness_none_latest_date_is_skipped_not_errored():
    """0行のテーブル(latest_date=None)は行数チェック側の責務なので、鮮度側は素通りする。"""
    today = datetime.date(2026, 8, 30)
    latest_dates = _full_latest_dates(today, scores=None)
    findings = check_table_freshness(latest_dates, today, edgar_enabled=True, fred_enabled=True)
    assert [f for f in findings if f.detail.get("table") == "scores"] == []


def test_macro_series_staleness_skipped_when_fred_disabled():
    today = datetime.date(2026, 8, 30)
    latest_dates = _full_latest_dates(today, macro_series=today - datetime.timedelta(days=999))
    findings = check_table_freshness(latest_dates, today, edgar_enabled=True, fred_enabled=False)
    assert [f for f in findings if f.detail.get("table") == "macro_series"] == []


def test_macro_series_staleness_flagged_when_fred_enabled():
    today = datetime.date(2026, 8, 30)
    latest_dates = _full_latest_dates(today, macro_series=today - datetime.timedelta(days=999))
    findings = check_table_freshness(latest_dates, today, edgar_enabled=True, fred_enabled=True)
    assert len(findings) == 1
    assert findings[0].detail["table"] == "macro_series"


# --- wrap_quarantine_findings ---------------------------------------------------


def test_wrap_quarantine_findings_preserves_logic_and_adds_remedy():
    original = [
        HealthFinding(
            code="quarantine_ratio_high",
            severity="error",
            message="quarantine ratio critically high: 100.0% (5312/5312)",
            detail={"ratio": 1.0, "quarantined": 5312, "universe_size": 5312},
        )
    ]
    wrapped = wrap_quarantine_findings(original)
    assert len(wrapped) == 1
    assert isinstance(wrapped[0], DoctorFinding)
    assert wrapped[0].code == "quarantine_ratio_high"
    assert wrapped[0].severity == "error"
    assert wrapped[0].message == original[0].message
    assert wrapped[0].detail == original[0].detail
    assert wrapped[0].remedy  # 空でないこと


# --- check_last_pipeline_run -----------------------------------------------------


def test_no_run_history_yields_no_findings():
    now = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)
    findings = check_last_pipeline_run(
        run_date=None, status=None, started_at=None, finished_at=None, health=None, now=now
    )
    assert findings == []


def test_orphaned_run_detected_after_six_hours():
    now = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)
    started_at = now - datetime.timedelta(hours=7)
    findings = check_last_pipeline_run(
        run_date=datetime.date(2026, 8, 30),
        status="running",
        started_at=started_at,
        finished_at=None,
        health=None,
        now=now,
    )
    assert len(findings) == 1
    assert findings[0].code == "run_orphaned"
    assert findings[0].severity == "error"


def test_running_within_six_hours_is_not_orphaned():
    now = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)
    started_at = now - datetime.timedelta(hours=2)
    findings = check_last_pipeline_run(
        run_date=datetime.date(2026, 8, 30),
        status="running",
        started_at=started_at,
        finished_at=None,
        health=None,
        now=now,
    )
    assert findings == []


def test_failed_run_reported():
    now = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)
    findings = check_last_pipeline_run(
        run_date=datetime.date(2026, 8, 30),
        status="failed",
        started_at=now - datetime.timedelta(hours=1),
        finished_at=now,
        health=[],
        now=now,
    )
    assert len(findings) == 1
    assert findings[0].code == "run_failed"
    assert findings[0].severity == "error"


def test_health_entries_surfaced_with_remedy():
    now = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)
    findings = check_last_pipeline_run(
        run_date=datetime.date(2026, 8, 30),
        status="degraded",
        started_at=now - datetime.timedelta(hours=1),
        finished_at=now,
        health=[
            {
                "code": "scoring_yield_dropped",
                "severity": "warning",
                "message": "スコア付与数が前回実行から大きく減少しました",
                "detail": {"scored": 10, "previous_scored": 5000},
            }
        ],
        now=now,
    )
    assert len(findings) == 1
    assert findings[0].code == "scoring_yield_dropped"
    assert findings[0].remedy  # 空でないこと


def test_unknown_health_code_gets_default_remedy():
    now = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)
    findings = check_last_pipeline_run(
        run_date=datetime.date(2026, 8, 30),
        status="degraded",
        started_at=now - datetime.timedelta(hours=1),
        finished_at=now,
        health=[{"code": "some_future_code", "severity": "warning", "message": "m", "detail": {}}],
        now=now,
    )
    assert len(findings) == 1
    assert findings[0].remedy


# --- DoctorReport.ok / format_doctor_report --------------------------------------


def test_ok_is_false_when_any_error_present():
    findings = [
        DoctorFinding(code="a", severity="warning", message="m1", detail={}, remedy="r1"),
        DoctorFinding(code="b", severity="error", message="m2", detail={}, remedy="r2"),
    ]
    ok = not any(f.severity == "error" for f in findings)
    report = DoctorReport(ok=ok, findings=findings)
    assert report.ok is False


def test_ok_is_true_when_only_warnings_present():
    findings = [DoctorFinding(code="a", severity="warning", message="m1", detail={}, remedy="r1")]
    ok = not any(f.severity == "error" for f in findings)
    report = DoctorReport(ok=ok, findings=findings)
    assert report.ok is True


def test_ok_is_true_when_no_findings():
    report = DoctorReport(ok=True, findings=[])
    assert report.ok is True


def test_format_doctor_report_includes_code_message_and_remedy():
    report = DoctorReport(
        ok=False,
        findings=[DoctorFinding(code="table_empty", severity="error", message="scores が0行です。", detail={}, remedy="uv run ...")],
    )
    text = format_doctor_report(report)
    assert "table_empty" in text
    assert "scores が0行です。" in text
    assert "uv run ..." in text
    assert "NG" in text


def test_format_doctor_report_ok_with_no_findings():
    report = DoctorReport(ok=True, findings=[])
    text = format_doctor_report(report)
    assert "OK" in text
