"""Form 4(I-3)と FINRA 空売り残高(I-5)のパースのテスト。純粋関数。"""

import datetime

from autoscreener.collectors.form4_source import build_insider_signal, parse_form4
from autoscreener.collectors.short_interest_source import parse_short_interest

_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-03-10</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>5.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>60000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-03-12</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>4000</value></transactionShares>
        <transactionPricePerShare><value>5.20</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2025-03-15</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>50000</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def test_parse_form4_extracts_nonderivative_transactions_with_relationship():
    txns = parse_form4(_FORM4_XML)
    assert len(txns) == 3
    buy = txns[0]
    assert buy.code == "P"
    assert buy.shares == 10000
    assert buy.price_per_share == 5.0
    assert buy.acquired is True
    assert buy.is_director and buy.is_officer and buy.officer_title == "CEO"
    assert buy.shares_owned_following == 60000


def test_build_insider_signal_nets_open_market_buys_minus_sells_only():
    txns = parse_form4(_FORM4_XML)
    sig = build_insider_signal(txns, as_of=datetime.date(2025, 3, 31))
    # P 10000 - S 4000 = 6000 net。M(オプション行使)は無視。
    assert sig.net_open_market_buy_shares == 6000
    assert sig.net_open_market_buy_usd == 10000 * 5.0
    assert sig.buy_transaction_count == 1
    assert sig.sell_transaction_count == 1


def test_insider_signal_respects_lookback_window():
    txns = parse_form4(_FORM4_XML)
    sig = build_insider_signal(txns, as_of=datetime.date(2026, 1, 1), lookback_days=30)
    assert sig.net_open_market_buy_shares == 0  # 全部窓の外


def test_insider_signal_normalized_by_market_cap():
    txns = parse_form4(_FORM4_XML)
    sig = build_insider_signal(txns, as_of=datetime.date(2025, 3, 31))
    assert sig.normalized_by_market_cap(1_000_000) == (10000 * 5.0) / 1_000_000
    assert sig.normalized_by_market_cap(None) is None


def test_parse_form4_bad_xml_returns_empty():
    assert parse_form4(b"<not-xml") == []


_SI_FILE = (
    "settlementDate|symbolCode|issueName|currentShortPositionQuantity|"
    "previousShortPositionQuantity|averageDailyVolumeQuantity|daysToCoverQuantity\n"
    "2026-08-15|ABCD|Acme Micro|1500000|1200000|300000|5.0\n"
    "2026-08-15|EFGH|Beta Co|900000|1000000|0|\n"
    "2026-08-15||No Symbol|100|100|100|1.0\n"
)


def test_parse_short_interest_maps_columns_by_header():
    records = parse_short_interest(_SI_FILE)
    assert [r.symbol for r in records] == ["ABCD", "EFGH"]
    abcd = records[0]
    assert abcd.settlement_date == datetime.date(2026, 8, 15)
    assert abcd.current_short_shares == 1_500_000
    assert abcd.days_to_cover() == 5.0
    assert abcd.short_interest_change() == 0.25


def test_days_to_cover_falls_back_to_adv_when_not_reported():
    records = parse_short_interest(_SI_FILE)
    efgh = records[1]
    assert efgh.reported_days_to_cover is None
    assert efgh.days_to_cover(fallback_adv_shares=450_000) == 2.0
