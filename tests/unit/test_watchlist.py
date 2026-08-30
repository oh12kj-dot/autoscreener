"""Tier 2(監視対象リスト)の判定ロジックのテスト(15.5)。"""

from autoscreener.screening.watchlist import (
    INSUFFICIENT_DATA,
    NEGATIVE_OUTLOOK,
    RECENT_LISTING,
    SINGLE_GATE_MISS,
    GateOutcome,
    build_tier2,
    classify_excluded,
)


def _excluded(ticker_id: int, *reasons: str) -> GateOutcome:
    return GateOutcome(ticker_id=ticker_id, included=False, exclusion_reasons=list(reasons))


def _included(ticker_id: int) -> GateOutcome:
    return GateOutcome(ticker_id=ticker_id, included=True, exclusion_reasons=[])


def test_single_watchable_gate_is_classified_as_watchlist():
    result = classify_excluded(_excluded(1, "liquidity_floor"))
    assert result is not None
    reason, detail = result
    assert reason == SINGLE_GATE_MISS
    assert "売買代金" in detail


def test_two_failed_gates_is_not_watchable():
    """「あと一歩」なのは1つだけ落とした銘柄。2つ落ちていれば復帰は近くない。"""
    assert classify_excluded(_excluded(1, "liquidity_floor", "negative_equity")) is None


def test_structural_gates_are_not_watchable():
    """時価総額上限・売上高上限・セクター除外は改善を期待する類の条件ではない。

    仮に基準内へ縮小したとしても、それは監視の成果ではなく事業の悪化を意味する
    (15.6:大きすぎる企業は算数上10倍になれない。セクターは変わらない)。
    """
    for reason in ("market_cap_ceiling", "revenue_ceiling", "excluded_sector"):
        assert classify_excluded(_excluded(1, reason)) is None


def test_missing_data_gates_are_not_watchable():
    """`missing_*`・`no_raw_data` は「あと一歩」ではなく「何も言えない」状態。"""
    for reason in ("missing_market_cap", "missing_revenue", "no_raw_data"):
        assert classify_excluded(_excluded(1, reason)) is None


def test_insufficient_listing_history_gets_its_own_label():
    """10章:IPO直後銘柄は除外せず追跡するため、他のゲート未達とは別ラベルにする。"""
    result = classify_excluded(_excluded(1, "insufficient_listing_history"))
    assert result is not None
    assert result[0] == RECENT_LISTING


def test_included_but_unscored_becomes_insufficient_data():
    """ゲートを通過してもモデルの必須入力が欠ければスコアは付かない。

    27章の方針:欠損は「スコアが低い」ではなく「測れない」。低いスコアを付けて
    順位に混ぜると、データ欠損が悪材料として誤読される。
    """
    entries = build_tier2(gates=[_included(7)], ranked_ticker_ids=set())
    assert len(entries) == 1
    assert entries[0].reason == INSUFFICIENT_DATA
    assert entries[0].ticker_id == 7


def test_included_and_scored_is_not_on_the_watchlist():
    """スコアが付いた銘柄は Tier 1(ランキング)に出るので Tier 2 には載せない。"""
    assert build_tier2(gates=[_included(7)], ranked_ticker_ids={7}) == []


def test_build_tier2_mixes_gate_misses_and_unscored():
    entries = build_tier2(
        gates=[
            _excluded(1, "liquidity_floor"),
            _excluded(2, "insufficient_listing_history"),
            _excluded(3, "market_cap_ceiling"),
            _included(4),
            _included(5),
            _included(6),
        ],
        ranked_ticker_ids={5},
        negative_outlook_ticker_ids={6},
    )
    by_ticker = {e.ticker_id: e.reason for e in entries}
    assert by_ticker == {
        1: SINGLE_GATE_MISS,
        2: RECENT_LISTING,
        4: INSUFFICIENT_DATA,
        6: NEGATIVE_OUTLOOK,
    }
    assert 3 not in by_ticker  # 構造的なゲートは監視対象外
    assert 5 not in by_ticker  # 順位が付いた銘柄は Tier 1


def test_gate_name_is_exposed_for_filtering():
    """UIで「流動性だけが足りない銘柄」に絞り込めるよう、機械可読な形でも持つ。"""
    entries = build_tier2(gates=[_excluded(1, "dilution_ceiling")], ranked_ticker_ids=set())
    assert entries[0].gate == "dilution_ceiling"


def test_measured_but_negative_outlook_is_not_reported_as_missing_data():
    """27.20:「測った結果がこうだった」を「測れなかった」と表示しない。

    実データではランキング外375銘柄のうち267件(71%)が見通しマイナスであり、
    これを「データ不足」と表示するのは誤情報になる。期待倍率0.7倍という事実は、
    データが無いことより遥かに有用な情報である。
    """
    entries = build_tier2(
        gates=[_included(1)], ranked_ticker_ids=set(), negative_outlook_ticker_ids={1}
    )
    assert len(entries) == 1
    assert entries[0].reason == NEGATIVE_OUTLOOK
    assert "期待倍率" in entries[0].detail
