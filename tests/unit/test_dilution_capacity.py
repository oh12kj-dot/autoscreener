"""tests/unit/test_dilution_capacity.py(K-4)。

`screening/dilution_capacity.py` は純関数のみなのでネットワーク・DBに一切
出ない。フィクスチャは実物のS-3表紙・10-QのATM記述・デススパイラル条項に
近い文面を定数として置く。誤検出しないことのテスト(紛らわしいが該当しない
文)を必ず含める。
"""

from __future__ import annotations

from autoscreener.screening.dilution_capacity import (
    detect_variable_conversion,
    options_ratio,
    parse_atm_capacity,
    parse_shelf_capacity,
    parse_usd_amount,
)

# --- parse_usd_amount --------------------------------------------------------


def test_parse_usd_amount_plain_comma_integer():
    assert parse_usd_amount("$150,000,000") == 150_000_000.0


def test_parse_usd_amount_million_suffix():
    assert parse_usd_amount("$150.0 million") == 150_000_000.0


def test_parse_usd_amount_billion_suffix():
    assert parse_usd_amount("$1.2 billion") == 1_200_000_000.0


def test_parse_usd_amount_ignores_footnote_marker():
    # 脚注記号 "(1)" は数値パターンの外なので無視される。
    assert parse_usd_amount("$150,000,000(1)") == 150_000_000.0


def test_parse_usd_amount_returns_none_when_absent():
    assert parse_usd_amount("no dollar amount here") is None


# --- parse_shelf_capacity -----------------------------------------------------

_S3_COVER_PAGE = """
CALCULATION OF REGISTRATION FEE
This prospectus relates to shares of our common stock, par value $0.0001 per
share, having an aggregate offering price of up to $75,000,000 that we may
offer and sell from time to time in one or more offerings. The underwriting
discount will be $1,500,000 assuming a per share offering price of $5.00.
"""


def test_parse_shelf_capacity_true_positive():
    result = parse_shelf_capacity(_S3_COVER_PAGE)
    assert result is not None
    assert result.amount_usd == 75_000_000.0
    assert "up to $75,000,000" in result.evidence


def test_parse_shelf_capacity_ignores_unrelated_leading_dollar_amount():
    # 本文中で最初に登場する $ 金額は par value(無関係)。これを誤って
    # 拾わないことを確認する(「最初に出てきた$金額を取るのは誤り」)。
    result = parse_shelf_capacity(_S3_COVER_PAGE)
    assert result is not None
    assert result.amount_usd != 0.0001


def test_parse_shelf_capacity_returns_none_without_trigger_words():
    text = (
        "The Company's common stock is listed on Nasdaq and last traded at "
        "approximately $5.00 per share on August 28, 2026."
    )
    assert parse_shelf_capacity(text) is None


def test_parse_shelf_capacity_million_suffix_form():
    text = "We are registering securities with an aggregate offering price of $150.0 million."
    result = parse_shelf_capacity(text)
    assert result is not None
    assert result.amount_usd == 150_000_000.0


# --- parse_atm_capacity -------------------------------------------------------

_ATM_AUTHORIZATION_TEXT = """
On March 3, 2026, we entered into a Sales Agreement establishing an
at-the-market offering program (the "ATM Program") under which we may offer
and sell shares of our common stock having an aggregate offering price of up
to $50,000,000 from time to time through the Sales Agent.
"""

_ATM_CONSUMPTION_TEXT = """
During the three months ended June 30, 2026, under the ATM Program we sold
1,234,567 shares of our common stock for aggregate gross proceeds of
approximately $12,345,678, before deducting commissions and offering
expenses payable by us.
"""


def test_parse_atm_capacity_authorized_amount():
    result = parse_atm_capacity(_ATM_AUTHORIZATION_TEXT)
    assert result is not None
    assert result.authorized_usd == 50_000_000.0
    assert "authorized" in result.evidence


def test_parse_atm_capacity_sold_amount_and_remaining():
    combined = _ATM_AUTHORIZATION_TEXT + "\n" + _ATM_CONSUMPTION_TEXT
    result = parse_atm_capacity(combined)
    assert result is not None
    assert result.authorized_usd == 50_000_000.0
    assert result.sold_usd == 12_345_678.0
    assert result.remaining_usd == 50_000_000.0 - 12_345_678.0
    assert "sold" in result.evidence


def test_parse_atm_capacity_none_when_atm_not_mentioned():
    text = (
        "We completed an underwritten public offering of 5,000,000 shares "
        "for gross proceeds of $25,000,000 in March 2026."
    )
    assert parse_atm_capacity(text) is None


def test_parse_atm_capacity_remaining_none_without_sold_amount():
    result = parse_atm_capacity(_ATM_AUTHORIZATION_TEXT)
    assert result is not None
    assert result.sold_usd is None
    assert result.remaining_usd is None


# --- detect_variable_conversion ------------------------------------------------

_DEATH_SPIRAL_TEXT = """
The Notes are convertible into shares of our common stock at a conversion
price equal to 80% of the lowest volume weighted average price of our common
stock during the 10 trading days immediately preceding the conversion date.
"""

_FIXED_CONVERSION_TEXT = """
The Notes are convertible into shares of our common stock at a fixed
conversion price of $5.00 per share, subject to customary anti-dilution
adjustments for stock splits and stock dividends.
"""


def test_detect_variable_conversion_true_positive():
    finding = detect_variable_conversion(_DEATH_SPIRAL_TEXT)
    assert finding is not None
    assert "80%" in finding.evidence
    assert "lowest" in finding.evidence.lower()


def test_detect_variable_conversion_explicit_phrase():
    finding = detect_variable_conversion("The debentures carry a variable conversion price.")
    assert finding is not None


def test_detect_variable_conversion_false_positive_on_fixed_price():
    # 固定転換価格(通常の転換社債)には反応しないことを確認する。
    assert detect_variable_conversion(_FIXED_CONVERSION_TEXT) is None


# --- options_ratio -------------------------------------------------------------


def test_options_ratio_basic():
    assert options_ratio(1_000_000.0, 20_000_000.0) == 0.05


def test_options_ratio_none_when_shares_outstanding_missing():
    assert options_ratio(1_000_000.0, None) is None


def test_options_ratio_none_when_shares_outstanding_zero_or_negative():
    assert options_ratio(1_000_000.0, 0.0) is None
    assert options_ratio(1_000_000.0, -5.0) is None


def test_options_ratio_none_when_unexercised_missing_or_negative():
    assert options_ratio(None, 20_000_000.0) is None
    assert options_ratio(-1.0, 20_000_000.0) is None
