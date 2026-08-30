"""K-7:`collectors/short_interest_fetch.py` のテスト。ネットワークに出ない
——`responses` でHTTPをモックする(`tests/unit/test_edgar_client.py` と同じ流儀)。"""

from __future__ import annotations

import datetime

import responses

from autoscreener.collectors.short_interest_fetch import (
    _BASE_URL,
    _candidate_settlement_targets,
    fetch_short_interest_all,
)

# 実際にWebFetchで確認したFINRAファイルのヘッダ・列名に合わせたパイプ区切り
# (拡張子は.csvだが実体はパイプ区切り——モジュールdocstring参照)。
_FILE_BODY = (
    "accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|"
    "marketClassCode|currentShortPositionQuantity|previousShortPositionQuantity|"
    "stockSplitFlag|averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag|"
    "changePercent|changePreviousNumber|settlementDate\n"
    "20260731|ABCD|Acme Micro Inc|A|NYSE|1500000|1200000||300000|5.0||25.0|300000|2026-07-31\n"
    "20260731|EFGH|Beta Co|A|NASDAQ|900000|1000000||450000||||-100000|2026-07-31\n"
)


def _url_for(date: datetime.date) -> str:
    return _BASE_URL.format(date=date.strftime("%Y%m%d"))


def test_candidate_settlement_targets_prefers_recent_half_month_anchors():
    reference = datetime.date(2026, 8, 30)
    targets = _candidate_settlement_targets(reference, periods=4)
    # すべて基準日以前で、新しい順。典型的なアンカー(月末・15日)を含む。
    assert targets == sorted(targets, reverse=True)
    assert all(t <= reference for t in targets)
    assert datetime.date(2026, 8, 15) in targets
    assert datetime.date(2026, 7, 31) in targets


def test_candidate_settlement_targets_respects_periods_count():
    reference = datetime.date(2026, 8, 30)
    assert len(_candidate_settlement_targets(reference, periods=3)) == 3
    assert len(_candidate_settlement_targets(reference, periods=6)) == 6


@responses.activate
def test_fetch_short_interest_all_indexes_by_symbol():
    """狙いの決済日(2026-07-31)にファイルがそのままある場合、404探索なしで
    取れて、シンボルごとの辞書に索かれること。"""
    target = datetime.date(2026, 7, 31)
    # periods=1 なので候補は target(=2026-07-31)一つだけ。offset=0で即座に
    # 当たるので、他の日付へのリクエストは発生しない
    # (`responses` は未消費の登録があると activate 終了時に失敗するため、
    # ここでは実際に叩かれる1件だけを登録する)。
    responses.add(responses.GET, _url_for(target), body=_FILE_BODY, status=200)

    by_symbol = fetch_short_interest_all(reference_date=target, periods=1)

    assert set(by_symbol.keys()) == {"ABCD", "EFGH"}
    abcd = by_symbol["ABCD"][0]
    assert abcd.settlement_date == datetime.date(2026, 7, 31)
    assert abcd.current_short_shares == 1_500_000
    assert abcd.days_to_cover() == 5.0


@responses.activate
def test_fetch_short_interest_all_falls_back_to_earlier_day_on_404():
    """狙った当日(月末・15日)が休場等で404の場合、数日遡って見つけること。"""
    target = datetime.date(2026, 8, 15)
    actual_file_date = target - datetime.timedelta(days=2)  # 週末で2日ずれた想定
    responses.add(responses.GET, _url_for(target), status=404)
    responses.add(responses.GET, _url_for(target - datetime.timedelta(days=1)), status=404)
    responses.add(responses.GET, _url_for(actual_file_date), body=_FILE_BODY, status=200)

    by_symbol = fetch_short_interest_all(reference_date=target, periods=1)
    assert "ABCD" in by_symbol


@responses.activate
def test_fetch_short_interest_all_returns_empty_dict_when_nothing_found():
    reference = datetime.date(2026, 8, 15)
    # 探索窓に入るURLをすべて404にする。periods=1なのでアンカーは1つだけ。
    targets = _candidate_settlement_targets(reference, periods=1)
    for target in targets:
        for offset in range(6):
            d = target - datetime.timedelta(days=offset)
            responses.add(responses.GET, _url_for(d), status=404)

    by_symbol = fetch_short_interest_all(reference_date=reference, periods=1)
    assert by_symbol == {}
