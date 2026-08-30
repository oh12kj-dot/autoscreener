from autoscreener.validation.rules import detect_day_over_day_spike, sanitize_info, validate_info


def test_valid_info_passes():
    info = {
        "marketCap": 1_000_000_000,
        "sharesOutstanding": 100_000_000,
        "currentPrice": 10.0,
        "grossMargins": 0.45,
        "operatingMargins": 0.12,
        "totalRevenue": 500_000_000,
        "currency": "USD",
        "financialCurrency": "USD",
    }
    result = validate_info(info)
    assert result.is_valid
    assert result.errors == []


def test_negative_market_cap_flagged():
    result = validate_info({"marketCap": -1})
    assert not result.is_valid
    assert "negative_market_cap" in result.errors


def test_margin_out_of_range_flagged():
    # 100%を大幅に超える粗利率(単位変換ミスの典型例)
    result = validate_info({"grossMargins": 12.5, "totalRevenue": 100})
    assert "grossMargins_out_of_range" in result.errors


def test_zero_gross_margin_with_revenue_suspected_missing():
    # 13.5: 売上があるのに粗利率がちょうど0.0は欠損の疑いが強い
    result = validate_info({"grossMargins": 0.0, "totalRevenue": 100})
    assert "grossMargins_zero_suspected_missing" in result.errors


def test_zero_gross_margin_without_revenue_is_not_flagged_as_out_of_range():
    # 売上自体が観測できていない場合は「本当にゼロかもしれない」ため
    # out_of_range 判定はしない(誤検知を避ける)
    result = validate_info({"grossMargins": 0.0})
    assert "grossMargins_out_of_range" not in result.errors


def test_market_cap_price_shares_mismatch_flagged():
    result = validate_info(
        {
            "marketCap": 10_000_000_000,  # $10B
            "sharesOutstanding": 100_000_000,
            "currentPrice": 10.0,  # implied market cap = $1B, 10x off
        }
    )
    assert "market_cap_price_shares_mismatch" in result.errors


def test_market_cap_price_shares_within_tolerance_not_flagged():
    result = validate_info(
        {
            "marketCap": 1_050_000_000,
            "sharesOutstanding": 100_000_000,
            "currentPrice": 10.0,  # implied = $1B, 5% off — within tolerance
        }
    )
    assert "market_cap_price_shares_mismatch" not in result.errors


def test_dual_class_share_count_not_flagged_as_mismatch():
    """`sharesOutstanding`が1クラス分しか返らないデュアルクラス銘柄は、
    marketCapが2倍前後になるのが正常。桁違いでない限り異常としない
    (異常判定するとsanitize_infoがmarketCapを落とし、ゲートで除外される)。"""
    result = validate_info(
        {
            "marketCap": 2_000_000_000,
            "sharesOutstanding": 100_000_000,  # 議決権クラス分が含まれていない
            "currentPrice": 10.0,  # implied = $1B
        }
    )
    assert "market_cap_price_shares_mismatch" not in result.errors


def test_currency_mismatch_flagged():
    result = validate_info({"currency": "USD", "financialCurrency": "JPY"})
    assert "currency_mismatch" in result.errors


def test_negative_revenue_flagged():
    result = validate_info({"totalRevenue": -100})
    assert "negative_revenue" in result.errors


def test_day_over_day_spike_detected():
    assert detect_day_over_day_spike("totalRevenue", 100.0, 1_500.0) == "totalRevenue_day_over_day_spike"
    assert detect_day_over_day_spike("totalRevenue", 100.0, 5.0) == "totalRevenue_day_over_day_spike"


def test_day_over_day_normal_change_not_flagged():
    assert detect_day_over_day_spike("totalRevenue", 100.0, 110.0) is None


def test_day_over_day_handles_none_and_zero_previous():
    assert detect_day_over_day_spike("totalRevenue", None, 100.0) is None
    assert detect_day_over_day_spike("totalRevenue", 0.0, 100.0) is None


# --- sanitize_info (2026-08-24) ----------------------------------------------


def test_sanitize_info_no_errors_returns_same_dict():
    info = {"marketCap": 1000.0}
    assert sanitize_info(info) is info


def test_sanitize_info_nulls_market_cap_on_negative_market_cap():
    info = {"marketCap": -1.0, "totalRevenue": 500.0}
    sanitized = sanitize_info(info)
    assert sanitized["marketCap"] is None
    assert sanitized["totalRevenue"] == 500.0
    assert info["marketCap"] == -1.0  # 元のdictは変更しない


def test_sanitize_info_nulls_market_cap_on_price_shares_mismatch():
    info = {"marketCap": 10_000_000_000, "sharesOutstanding": 100_000_000, "currentPrice": 10.0}
    sanitized = sanitize_info(info)
    assert sanitized["marketCap"] is None


def test_sanitize_info_nulls_total_revenue_on_negative_revenue():
    info = {"totalRevenue": -1.0}
    sanitized = sanitize_info(info)
    assert sanitized["totalRevenue"] is None


def test_sanitize_info_nulls_gross_margin_on_suspected_missing():
    info = {"grossMargins": 0.0, "totalRevenue": 500.0}
    sanitized = sanitize_info(info)
    assert sanitized["grossMargins"] is None
    assert sanitized["totalRevenue"] == 500.0


def test_sanitize_info_ignores_unrelated_error_codes():
    # operatingMargins_out_of_range はNone化対象外(どのゲートからも参照されない)
    info = {"marketCap": 1000.0, "operatingMargins": -10.0}
    assert sanitize_info(info) == info


def test_sanitize_info_uses_current_rules_not_stored_errors():
    """保存済みのvalidation_errorsではなく、常に現行の検証ロジックで再導出する。
    検証閾値を直しても、内容が変わらない限り古い判定が残り続ける問題への対処。"""
    # 旧閾値(±50%)なら mismatch 扱いだったが、現行の3倍基準では正常
    info = {"marketCap": 2_000_000_000, "sharesOutstanding": 100_000_000, "currentPrice": 10.0}
    assert sanitize_info(info)["marketCap"] == 2_000_000_000
