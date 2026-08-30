from autoscreener.collectors.universe_source import (
    CandidateTicker,
    filter_candidates,
    parse_nasdaq_listed,
    parse_other_listed,
)

NASDAQ_SAMPLE = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
AAAP|Pacer Barings CLO Market Flex ETF|G|N|N|100|Y|N
ZTEST|Test Company - Common Stock|G|Y|N|100|N|N
ZWRRT|Some Company - Warrant|G|N|N|100|N|N
File Creation Time: 0821202621:31|||||||"""

OTHER_SAMPLE = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
A|Agilent Technologies, Inc. Common Stock|N|A|N|100|N|A
AAA|Alternative Access First Priority CLO Bond ETF|P|AAA|Y|100|N|AAA
BRK.B|Berkshire Hathaway Inc. Common Stock|N|BRK.B|N|100|N|BRK.B
XYZ|Some AMEX Common Stock|A|XYZ|N|100|N|XYZ
NYETF|Some NYSE-listed ETF|N|NYETF|Y|100|N|NYETF
File Creation Time: 0821202621:31|||||||"""


def test_parse_nasdaq_listed_skips_footer_and_extracts_flags():
    rows = parse_nasdaq_listed(NASDAQ_SAMPLE)
    symbols = {r.symbol: r for r in rows}

    assert set(symbols) == {"AAPL", "AAAP", "ZTEST", "ZWRRT"}
    assert symbols["AAPL"].is_etf is False
    assert symbols["AAPL"].is_test_issue is False
    assert symbols["AAAP"].is_etf is True
    assert symbols["ZTEST"].is_test_issue is True


def test_parse_other_listed_filters_to_nyse_only():
    rows = parse_other_listed(OTHER_SAMPLE)
    symbols = {r.symbol for r in rows}

    # Exchange 'A' (AMEX) と 'P' (NYSE Arca) は除外 — NYSE('N')のみが対象(4章)
    assert "XYZ" not in symbols
    assert "AAA" not in symbols
    assert symbols == {"A", "BRK.B", "NYETF"}


def test_filter_candidates_excludes_etf_test_issue_and_non_common_stock():
    candidates = parse_nasdaq_listed(NASDAQ_SAMPLE) + parse_other_listed(OTHER_SAMPLE)
    filtered = filter_candidates(candidates)
    symbols = {c.symbol for c in filtered}

    assert "AAPL" in symbols
    assert "A" in symbols
    assert "BRK.B" in symbols
    assert "AAAP" not in symbols  # ETF
    assert "NYETF" not in symbols  # ETF (NYSE-listed, so exchange filter alone wouldn't catch it)
    assert "ZTEST" not in symbols  # test issue
    assert "ZWRRT" not in symbols  # warrant (non-common-stock name pattern)


def test_preferred_share_symbols_are_excluded_by_symbol_shape():
    """名称で判別できない優先株を、シンボルの形状("$")で落とす(28.19)。

    NASDAQ Trader のディレクトリでは優先株が "FITB$I" のように表される。
    実データでは、名称に "preferred" 等を含まない25銘柄がこのすり抜けで
    `tickers` に入り込み、日次収集の対象になり続けていた。普通株ではないので
    10バガー探索の対象として意味がないうえ、除外銘柄一覧に出るのに詳細が
    開けないという状態にもなっていた。
    """
    candidates = [
        CandidateTicker("FITB$I", "Fifth Third Bancorp Depositary Shares", "NYSE", False, False),
        CandidateTicker("AHL$D", "Aspen Insurance Holdings Ltd", "NYSE", False, False),
        CandidateTicker("AAPL", "Apple Inc. - Common Stock", "NASDAQ", False, False),
        CandidateTicker("BRK-B", "Berkshire Hathaway Inc. Class B", "NYSE", False, False),
    ]
    kept = {c.symbol for c in filter_candidates(candidates)}
    assert kept == {"AAPL", "BRK-B"}
