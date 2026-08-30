"""API層のテスト。

`docker compose up -d` で起動済みのローカル開発用Postgresに対して実行する
(このプロジェクトの他のテストと異なり、DBなしでは実行できない)。
専用のティッカーシンボル(ZZ***)でテストデータを作成し、各テストの終了時に
削除してクリーンアップする。
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from autoscreener.api.main import app
from autoscreener.config import load_scoring_config
from autoscreener.db.models import RawSnapshot, Score, Ticker, UniverseSnapshot
from autoscreener.db.session import session_scope

client = TestClient(app)

_TODAY = datetime.date(2099, 1, 1)  # 実データと衝突しない未来日付を使う
# API層は現行scoring_versionでフィルタする(新旧スコアの二重表示バグの修正、
# 2026-08-24)。テストデータもハードコードした値ではなく実際の設定に追随させる。
_SCORING_VERSION = load_scoring_config().scoring_version


def _cleanup(symbols: list[str]) -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all()
        for t in tickers:
            session.query(Score).filter_by(ticker_id=t.id).delete()
            session.query(UniverseSnapshot).filter_by(ticker_id=t.id).delete()
            session.query(RawSnapshot).filter_by(ticker_id=t.id).delete()
            session.delete(t)


@pytest.fixture
def seeded_candidate():
    symbol = "ZZTEST1"
    _cleanup([symbol])
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US", sector="Technology")
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=_TODAY,
                source="test",
                payload={"info": {"marketCap": 1_000_000_000, "currentPrice": 42.0, "sector": "Technology"}},
                content_hash="test-hash-1",
                last_seen_date=_TODAY,
                available_from=_TODAY,
                is_valid=True,
            )
        )
        session.add(UniverseSnapshot(snapshot_date=_TODAY, ticker_id=ticker.id, included=True, exclusion_reason=None))
        session.add(
            Score(
                ticker_id=ticker.id,
                score_date=_TODAY,
                scoring_version=_SCORING_VERSION,
                config_hash="test-config-hash",
                probability=0.0421,
                median_moic=3.2,
                log_moic_mu=1.163,
                log_moic_sigma=0.9,
                survival_probability=0.71,
                factors={"expected_moic": 4.8, "revenue_multiple": 2.4, "dilution_drag": 1.1},
            )
        )
    yield symbol
    _cleanup([symbol])


@pytest.fixture
def seeded_excluded():
    symbol = "ZZTEST2"
    _cleanup([symbol])
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US", sector="Financial Services")
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=_TODAY,
                source="test",
                payload={"info": {"marketCap": 5_000_000, "sector": "Financial Services"}},
                content_hash="test-hash-2",
                last_seen_date=_TODAY,
                available_from=_TODAY,
                is_valid=True,
            )
        )
        session.add(
            UniverseSnapshot(
                snapshot_date=_TODAY, ticker_id=ticker.id, included=False, exclusion_reason="excluded_sector,price_floor"
            )
        )
    yield symbol
    _cleanup([symbol])


@pytest.fixture
def seeded_stale_score():
    # 2026-08-24に発見したバグの回帰テスト:run_scoringが書いたScore行は、
    # 後からapply_gatesを同日中に再実行して判定が変わっても自動更新されない。
    # ticker自体はScoreを持つが、対象日のuniverse_snapshotsではincluded=False
    # という「ゲート再実行でスコアリング後に対象外になった」状態を再現する。
    symbol = "ZZTEST3"
    _cleanup([symbol])
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US", sector="Technology")
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=_TODAY,
                source="test",
                payload={"info": {"marketCap": 1_000_000_000, "currentPrice": 10.0, "sector": "Technology"}},
                content_hash="test-hash-3",
                last_seen_date=_TODAY,
                available_from=_TODAY,
                is_valid=True,
            )
        )
        session.add(
            UniverseSnapshot(snapshot_date=_TODAY, ticker_id=ticker.id, included=False, exclusion_reason="dilution_ceiling")
        )
        session.add(
            Score(
                ticker_id=ticker.id,
                score_date=_TODAY,
                scoring_version=_SCORING_VERSION,
                config_hash="test-config-hash",
                probability=0.0999,
                median_moic=5.0,
                log_moic_mu=1.609,
                log_moic_sigma=0.8,
                survival_probability=0.8,
                factors={"expected_moic": 6.9},
            )
        )
    yield symbol
    _cleanup([symbol])


@pytest.fixture
def seeded_mid_cap():
    """日次バッチのゲート(11.7B)は通るが、既定の目標(3.5B)には大きすぎる銘柄。

    29章で規模の上限が目標倍率の関数になったため、この状態が初めて起こりうる
    ようになった——以前はゲートを通った銘柄=必ず既定の目標の候補だった。
    """
    from test_moic import make_inputs

    symbol = "ZZTEST9"
    _cleanup([symbol])
    inputs = make_inputs(market_cap=5_000_000_000, revenue_latest=1_000_000_000, gross_profit_latest=5.0e8)
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US", sector="Technology")
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=_TODAY,
                source="test",
                payload={
                    "info": {
                        "marketCap": 5_000_000_000,
                        "totalRevenue": 1_000_000_000,
                        "currentPrice": 50.0,
                        "sector": "Technology",
                    }
                },
                content_hash="test-hash-9",
                last_seen_date=_TODAY,
                available_from=_TODAY,
                is_valid=True,
            )
        )
        session.add(
            UniverseSnapshot(snapshot_date=_TODAY, ticker_id=ticker.id, included=True, exclusion_reason=None)
        )
        session.add(
            Score(
                ticker_id=ticker.id,
                score_date=_TODAY,
                scoring_version=_SCORING_VERSION,
                config_hash="test-config-hash",
                probability=0.02,
                median_moic=2.5,
                log_moic_mu=0.9,
                log_moic_sigma=0.9,
                survival_probability=0.75,
                factors={"expected_moic": 3.1},
                inputs={
                    **inputs.to_dict(),
                    "cross_section": {
                        "median_log_momentum": 0.05,
                        "median_log_sigma": 0.9,
                        "sample_size": 100,
                        "median_dilution_cagr": 0.0,
                        "horizon_years": 7,
                    },
                },
            )
        )
    yield symbol
    _cleanup([symbol])


@pytest.fixture
def seeded_mid_cap_single_gate_miss():
    """流動性だけを落としている $5B の銘柄。

    既定の目標では規模でも外れるので「あと一歩(ゲート1つ未達)」ではない。
    目標を「3年で3倍」に緩めると、規模の条件は満たすので初めて「あと一歩」になる。
    """
    symbol = "ZZTEST8"
    _cleanup([symbol])
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US", sector="Technology")
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=_TODAY,
                source="test",
                payload={"info": {"marketCap": 5_000_000_000, "totalRevenue": 1_000_000_000}},
                content_hash="test-hash-8",
                last_seen_date=_TODAY,
                available_from=_TODAY,
                is_valid=True,
            )
        )
        session.add(
            UniverseSnapshot(
                snapshot_date=_TODAY,
                ticker_id=ticker.id,
                included=False,
                exclusion_reason="liquidity_floor",
            )
        )
    yield symbol
    _cleanup([symbol])


def test_watchlist_does_not_call_a_too_big_ticker_one_gate_away(
    seeded_mid_cap_single_gate_miss, seeded_candidate
):
    """29章:規模で外れる銘柄を「あと一歩」に混ぜない。

    `WATCHABLE_SINGLE_GATES` が規模の上限を除いているのは「改善して復帰する
    類の条件ではない」ため(15.6)。上限が目標の関数になった以上、その判定も
    目標ごとに行わないと、既定の目標では復帰しえない銘柄が監視リストに並ぶ。
    """
    default = client.get("/api/v1/watchlist", params={"reason": "single_gate_miss", "limit": 200}).json()
    assert seeded_mid_cap_single_gate_miss not in [item["ticker"] for item in default["items"]]

    loose = client.get(
        "/api/v1/watchlist",
        params={"reason": "single_gate_miss", "limit": 200, "horizon_years": 3, "target_moic": 3},
    ).json()
    assert seeded_mid_cap_single_gate_miss in [item["ticker"] for item in loose["items"]]


def test_target_spec_reports_the_ceiling_for_the_target():
    """29章:上限は目標の関数。APIはその目標で有効な上限を必ず返す。"""
    default = client.get("/api/v1/candidates", params={"limit": 1}).json()["target"]
    assert default["market_cap_ceiling"] == 3_500_000_000
    assert default["revenue_ceiling"] == 3_000_000_000
    assert default["universe_ceiling_capped"] is False

    loose = client.get(
        "/api/v1/candidates", params={"limit": 1, "horizon_years": 3, "target_moic": 3}
    ).json()["target"]
    assert loose["market_cap_ceiling"] > default["market_cap_ceiling"]
    assert loose["universe_ceiling_capped"] is False


def test_target_spec_flags_when_the_universe_stops_widening():
    """materialize 範囲(3倍)より緩い目標では上限が頭打ちになることを明示する。"""
    body = client.get(
        "/api/v1/candidates", params={"limit": 1, "horizon_years": 2, "target_moic": 1.5}
    ).json()["target"]
    assert body["universe_ceiling_capped"] is True


def test_mid_cap_is_ranked_only_when_the_target_is_loose_enough(seeded_mid_cap):
    """$5Bの銘柄は「7年で10倍」には出ず、「3年で3倍」には出る(29章)。"""
    default = client.get(
        "/api/v1/candidates", params={"date": _TODAY.isoformat(), "limit": 200}
    ).json()
    assert seeded_mid_cap not in [item["ticker"] for item in default["items"]]

    loose = client.get(
        "/api/v1/candidates",
        params={"date": _TODAY.isoformat(), "limit": 200, "horizon_years": 3, "target_moic": 3},
    ).json()
    assert seeded_mid_cap in [item["ticker"] for item in loose["items"]]


def test_mid_cap_detail_reports_the_target_specific_exclusion(seeded_mid_cap):
    """ランキングに出ない銘柄の詳細が「候補です」と表示されないこと(29章)。"""
    default = client.get(f"/api/v1/candidates/{seeded_mid_cap}").json()
    assert default["is_candidate"] is False
    assert default["exclusion_reason"] == ["market_cap_ceiling"]
    # 保存済みの確率は「小型株のプールに属していたと仮定した値」なので出さない。
    assert default["probability"] is None
    assert default["expected_moic"] is None
    assert default["score_history"] == []

    loose = client.get(
        f"/api/v1/candidates/{seeded_mid_cap}", params={"horizon_years": 3, "target_moic": 3}
    ).json()
    assert loose["is_candidate"] is True
    assert loose["exclusion_reason"] is None


def test_excluded_lists_tickers_that_are_too_big_for_the_target(seeded_mid_cap):
    """**ランキングにも除外一覧にも出ない銘柄を作らない**(29章)。"""
    default = client.get("/api/v1/excluded", params={"reason": "market_cap_ceiling", "limit": 200}).json()
    assert seeded_mid_cap in [item["ticker"] for item in default["items"]]

    loose = client.get(
        "/api/v1/excluded",
        params={"reason": "market_cap_ceiling", "limit": 200, "horizon_years": 3, "target_moic": 3},
    ).json()
    assert seeded_mid_cap not in [item["ticker"] for item in loose["items"]]


def test_list_candidates_excludes_stale_score_for_now_excluded_ticker(seeded_stale_score):
    response = client.get("/api/v1/candidates", params={"date": _TODAY.isoformat(), "limit": 200})
    assert response.status_code == 200
    symbols = [item["ticker"] for item in response.json()["items"]]
    assert seeded_stale_score not in symbols


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_candidate_detail_for_included_ticker(seeded_candidate):
    response = client.get(f"/api/v1/candidates/{seeded_candidate}")
    assert response.status_code == 200
    body = response.json()
    assert body["is_candidate"] is True
    assert body["exclusion_reason"] is None
    assert body["probability"] == 0.0421
    assert body["expected_moic"] == 4.8
    assert any(f["key"] == "revenue_multiple" for f in body["factor_breakdown"])
    assert body["survival_probability"] == 0.71
    # 希薄化は割り算で効くので、内訳では逆数(=MOICを何倍にしているか)で返す
    dilution = next(f for f in body["factor_breakdown"] if f["key"] == "dilution_drag")
    assert dilution["value"] == 1.1
    assert abs(dilution["contribution"] - 1 / 1.1) < 1e-9


def test_get_candidate_detail_for_excluded_ticker_returns_200_with_reason(seeded_excluded):
    response = client.get(f"/api/v1/candidates/{seeded_excluded}")
    assert response.status_code == 200
    body = response.json()
    assert body["is_candidate"] is False
    assert body["exclusion_reason"] == ["excluded_sector", "price_floor"]
    assert body["probability"] is None


def test_get_candidate_detail_unknown_ticker_returns_404():
    response = client.get("/api/v1/candidates/ZZNOTREAL9")
    assert response.status_code == 404


def test_get_candidate_detail_rejects_malformed_ticker():
    response = client.get("/api/v1/candidates/not_a_valid_ticker!!")
    assert response.status_code == 422


def test_list_excluded_filters_by_reason(seeded_excluded):
    response = client.get("/api/v1/excluded", params={"reason": "price_floor"})
    assert response.status_code == 200
    body = response.json()
    symbols = [item["ticker"] for item in body["items"]]
    assert seeded_excluded in symbols


def test_list_candidates_respects_limit():
    response = client.get("/api/v1/candidates", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) <= 5
    assert body["limit"] == 5


def test_list_candidates_rejects_limit_over_max():
    response = client.get("/api/v1/candidates", params={"limit": 9999})
    assert response.status_code == 422


def test_list_candidates_with_explicit_date(seeded_candidate):
    response = client.get("/api/v1/candidates", params={"date": _TODAY.isoformat()})
    assert response.status_code == 200
    body = response.json()
    assert body["score_date"] == _TODAY.isoformat()
    symbols = [item["ticker"] for item in body["items"]]
    assert seeded_candidate in symbols


def test_list_candidates_with_date_having_no_scores_returns_empty():
    response = client.get("/api/v1/candidates", params={"date": "1901-01-01"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_score_dates_includes_seeded_date(seeded_candidate):
    response = client.get("/api/v1/scores/dates")
    assert response.status_code == 200
    assert _TODAY.isoformat() in response.json()["dates"]


def test_universe_status_returns_expected_shape():
    response = client.get("/api/v1/universe/status")
    assert response.status_code == 200
    body = response.json()
    assert "collection_status_counts" in body
    assert "gate_status_counts" in body
    assert "scoring_status_counts" in body
    assert isinstance(body["universe_size"], int)


# --- GET /watchlist(Tier 2、15.5・26章) --------------------------------------


@pytest.fixture
def seeded_tier2():
    """Tier 2 の3パターンを同じ日付で用意する。

    - ZZT2GATE : 監視対象のゲートを1つだけ落とした銘柄
    - ZZT2IPO  : 決算実績の期数不足(新規上場ウォッチリスト)
    - ZZT2DATA : 全ゲート通過だがスコアが付かなかった銘柄(データ不足)
    - ZZT2SKIP : 構造的なゲート(時価総額上限)のみ未達 = 監視対象にしない
    - ZZT2OK   : 通常のTier 1銘柄(スコアあり)
    """
    symbols = ["ZZT2GATE", "ZZT2IPO", "ZZT2DATA", "ZZT2SKIP", "ZZT2OK"]
    _cleanup(symbols)
    with session_scope() as session:
        for i, (symbol, included, reason) in enumerate(
            [
                ("ZZT2GATE", False, "liquidity_floor"),
                ("ZZT2IPO", False, "insufficient_listing_history"),
                ("ZZT2DATA", True, None),
                ("ZZT2SKIP", False, "market_cap_ceiling"),
                ("ZZT2OK", True, None),
            ]
        ):
            ticker = Ticker(symbol=symbol, market="US", sector="Technology")
            session.add(ticker)
            session.flush()
            session.add(
                RawSnapshot(
                    ticker_id=ticker.id,
                    snapshot_date=_TODAY,
                    source="test",
                    payload={"info": {"shortName": f"{symbol} Inc.", "sector": "Technology"}},
                    content_hash=f"tier2-hash-{i}",
                    last_seen_date=_TODAY,
                    available_from=_TODAY,
                    is_valid=True,
                )
            )
            session.add(
                UniverseSnapshot(
                    snapshot_date=_TODAY, ticker_id=ticker.id, included=included, exclusion_reason=reason
                )
            )
            if symbol == "ZZT2OK":
                session.add(
                    Score(
                        ticker_id=ticker.id,
                        score_date=_TODAY,
                        scoring_version=_SCORING_VERSION,
                        config_hash="tier2",
                        probability=0.0055,
                        median_moic=1.5,
                        log_moic_mu=0.405,
                        log_moic_sigma=0.7,
                        survival_probability=0.6,
                        factors={"expected_moic": 1.9},
                    )
                )
    yield symbols
    _cleanup(symbols)


def test_watchlist_groups_entries_by_reason(seeded_tier2):
    response = client.get("/api/v1/watchlist?limit=200")
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot_date"] == _TODAY.isoformat()

    by_ticker = {item["ticker"]: item for item in body["items"]}
    assert by_ticker["ZZT2GATE"]["reason"] == "single_gate_miss"
    assert by_ticker["ZZT2GATE"]["gate"] == "liquidity_floor"
    assert by_ticker["ZZT2IPO"]["reason"] == "recent_listing"
    assert by_ticker["ZZT2DATA"]["reason"] == "insufficient_data"
    # 構造的なゲートのみ未達・Tier 1入り済みの銘柄は監視対象にしない
    assert "ZZT2SKIP" not in by_ticker
    assert "ZZT2OK" not in by_ticker


def test_watchlist_filters_by_reason_and_gate(seeded_tier2):
    body = client.get("/api/v1/watchlist?reason=recent_listing&limit=200").json()
    assert all(item["reason"] == "recent_listing" for item in body["items"])
    assert "ZZT2IPO" in {item["ticker"] for item in body["items"]}

    body = client.get("/api/v1/watchlist?gate=liquidity_floor&limit=200").json()
    assert all(item["gate"] == "liquidity_floor" for item in body["items"])
    assert "ZZT2IPO" not in {item["ticker"] for item in body["items"]}


def test_watchlist_reports_counts_by_reason_before_filtering(seeded_tier2):
    """タブに出す件数は絞り込み前の全体件数であること。"""
    body = client.get("/api/v1/watchlist?reason=recent_listing&limit=1").json()
    assert body["counts_by_reason"]["single_gate_miss"] >= 1
    assert body["counts_by_reason"]["insufficient_data"] >= 1
    assert body["counts_by_gate"]["liquidity_floor"] >= 1


@pytest.fixture
def seeded_negative_outlook():
    """27.20:測れたが期待倍率が1.0を下回った銘柄(確率NULL)。"""
    symbol = "ZZTEST9"
    _cleanup([symbol])
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US", sector="Industrials")
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=_TODAY,
                source="test",
                payload={"info": {"marketCap": 500_000_000, "currentPrice": 12.0, "sector": "Industrials"}},
                content_hash="test-hash-9",
                last_seen_date=_TODAY,
                available_from=_TODAY,
                is_valid=True,
            )
        )
        session.add(UniverseSnapshot(snapshot_date=_TODAY, ticker_id=ticker.id, included=True, exclusion_reason=None))
        session.add(
            Score(
                ticker_id=ticker.id,
                score_date=_TODAY,
                scoring_version=_SCORING_VERSION,
                config_hash="test-config-hash",
                probability=None,
                calibrated_on_pace_probability=None,
                median_moic=0.42,
                log_moic_mu=-0.87,
                log_moic_sigma=0.9,
                survival_probability=0.6,
                # 27.20:数値の因子と**文字列**のメタ情報が同じJSONに同居する
                factors={
                    "expected_moic": 0.52,
                    "revenue_multiple": 0.9,
                    "dilution_drag": 1.3,
                    "unranked_reason": "negative_outlook",
                },
            )
        )
    yield symbol
    _cleanup([symbol])


def test_negative_outlook_detail_serialises(seeded_negative_outlook):
    """**実際に踏んだバグのリグレッションテスト(28.19)。**

    `CandidateDetail.factors` を `dict[str, float]` にしていたため、
    `unranked_reason`(文字列)を含む行のレスポンス検証が失敗し、見通しマイナスの
    銘柄(実データで256件)の詳細が**全件500になっていた**。監視リストの
    「見通しがマイナス」タブからリンクを踏むと必ずエラーになる状態だった。
    """
    response = client.get(f"/api/v1/candidates/{seeded_negative_outlook}")
    assert response.status_code == 200
    body = response.json()
    assert body["probability"] is None
    assert body["unranked_reason"] == "negative_outlook"
    # 27.20:「測れなかった」ではなく「測った結果がこうだった」を出せていること
    assert body["expected_moic"] == pytest.approx(0.52)
    assert body["factors"]["unranked_reason"] == "negative_outlook"


def test_negative_outlook_never_reports_a_calibrated_probability(seeded_negative_outlook):
    """確率が無い以上、それを較正した値も存在しえない(28.19)。"""
    body = client.get(f"/api/v1/candidates/{seeded_negative_outlook}").json()
    assert body["calibrated_on_pace_probability"] is None


def test_negative_outlook_is_absent_from_the_ranking(seeded_negative_outlook):
    """順位を付けない銘柄がランキングに混ざらないこと(27.20)。"""
    body = client.get(f"/api/v1/candidates?date={_TODAY.isoformat()}").json()
    assert seeded_negative_outlook not in [item["ticker"] for item in body["items"]]


def test_watchlist_rejects_an_unknown_reason():
    """**タイプミスを「該当0件」として返さないこと**(28.19)。

    未知の値を黙って受けると、利用者には「そういう銘柄が無い」と読める。
    絞り込みの指定を間違えたのか、本当に0件なのかが区別できない。
    """
    assert client.get("/api/v1/watchlist?reason=bogus").status_code == 422
    assert client.get("/api/v1/watchlist?reason=negative_outlook").status_code == 200


def test_ticker_pattern_accepts_symbols_that_exist_in_the_database():
    """**一覧に出ている銘柄はすべて詳細を開けること**(28.19)。

    NYSEの優先株は "AHL$D" のように "$" を含む。以前のパターンはこれを弾いて
    いたため、除外銘柄一覧に載っている24銘柄をクリックすると422になり、
    一覧に出ているのに開けないという状態だった。
    存在しない銘柄は404(=形式は正しいが該当なし)であるべきで、422ではない。
    """
    response = client.get("/api/v1/candidates/AHL$D")
    assert response.status_code in (200, 404), response.text
    assert client.get("/api/v1/candidates/ZZNOPE").status_code == 404
    # 形式そのものが不正なものは引き続き422
    assert client.get("/api/v1/candidates/lowercase").status_code == 422


# --- 30.2 / 30.4:取扱可否・流動性・レッドフラグのAPI統合テスト -----------------

from autoscreener.dates import utc_today  # noqa: E402
from autoscreener.db.models import Filing, PriceSnapshot  # noqa: E402


def _cleanup_filings_and_prices(symbols: list[str]) -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all()
        for t in tickers:
            session.query(Filing).filter_by(ticker_id=t.id).delete()
            session.query(PriceSnapshot).filter_by(ticker_id=t.id).delete()


def test_candidate_summary_defaults_to_unknown_tradability_and_no_price_history(seeded_candidate):
    """config/tradability/ に何も無い開発環境では常に "unknown"(30.2.4)。"""
    body = client.get(f"/api/v1/candidates?date={_TODAY.isoformat()}").json()
    item = next(i for i in body["items"] if i["ticker"] == seeded_candidate)
    assert item["tradability"] == "unknown"
    assert item["tradable_brokers"] == []
    # price_snapshotsが無い銘柄はadv_usdがNoneになり、500にならないこと
    assert item["adv_usd"] is None
    assert item["blocking_flag_count"] == 0
    assert item["warning_flag_count"] == 0


def test_candidate_detail_includes_liquidity_and_tradability_fields(seeded_candidate):
    body = client.get(f"/api/v1/candidates/{seeded_candidate}").json()
    assert body["tradability"] == "unknown"
    assert body["adv_usd"] is None
    assert body["red_flags"] == []
    assert body["filings_checked_on"] is None


def test_adv_usd_matches_simple_mean_of_close_times_volume(seeded_candidate):
    """20営業日ぶんの close x volume の単純平均と一致すること(30.2.4)。"""
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=seeded_candidate).one()
        for i in range(20):
            session.add(
                PriceSnapshot(
                    ticker_id=ticker.id,
                    trade_date=_TODAY - datetime.timedelta(days=i),
                    close=10.0,
                    volume=1000,
                )
            )
    try:
        body = client.get(f"/api/v1/candidates/{seeded_candidate}").json()
        # detail は utc_today() 基準で直近40暦日を見るため、未来日付(_TODAY)の
        # 価格データはヒットしない可能性がある。list側は score_date(_TODAY)基準
        # で確実にヒットする。
        list_body = client.get(f"/api/v1/candidates?date={_TODAY.isoformat()}").json()
        item = next(i for i in list_body["items"] if i["ticker"] == seeded_candidate)
        assert item["adv_usd"] == pytest.approx(10_000.0)
        assert item["adv_observation_days"] == 20
        assert item["max_position_usd"] is not None
        assert item["position_binding_constraint"] in ("liquidity", "portfolio")
    finally:
        _cleanup_filings_and_prices([seeded_candidate])


def test_filings_endpoint_returns_empty_list_for_untracked_ticker(seeded_candidate):
    response = client.get(f"/api/v1/filings/{seeded_candidate}")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_filings_endpoint_404_for_unknown_ticker():
    assert client.get("/api/v1/filings/ZZNOPE").status_code == 404


def test_restatement_filing_produces_blocking_red_flag_and_summary_count(seeded_candidate):
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=seeded_candidate).one()
        session.add(
            Filing(
                ticker_id=ticker.id,
                cik="0000320193",
                accession_number="0001234567-26-000099",
                form="8-K",
                # ルート側は utc_today() を基準にする(dates.py:デプロイ先の
                # タイムゾーンに依存しない一貫した日付)。ローカル`date.today()`
                # はJST環境ではUTCより1日進みうるため、ここでも utc_today() を使う。
                filed_date=utc_today(),
                items=["4.02"],
                primary_document="a.htm",
                document_url="https://www.sec.gov/Archives/edgar/data/320193/x/a.htm",
            )
        )
    try:
        detail = client.get(f"/api/v1/candidates/{seeded_candidate}").json()
        assert any(f["code"] == "restatement" and f["severity"] == "blocking" for f in detail["red_flags"])
        assert detail["filings_checked_on"] is not None

        filings = client.get(f"/api/v1/filings/{seeded_candidate}").json()
        assert filings["total"] == 1
        assert filings["items"][0]["form"] == "8-K"
    finally:
        _cleanup_filings_and_prices([seeded_candidate])


def test_tradable_only_excludes_unlisted_ticker_when_no_broker_lists_exist(seeded_candidate):
    """coverageが空(=全銘柄unknown)のとき、tradable_only=Trueは全件を除外する(30.2.4)。"""
    body = client.get(f"/api/v1/candidates?date={_TODAY.isoformat()}&tradable_only=true").json()
    assert seeded_candidate not in [item["ticker"] for item in body["items"]]


# --- 30.8:マクロ(FRED)API統合テスト --------------------------------------


def test_macro_endpoint_returns_disabled_without_api_key(monkeypatch):
    """FRED_API_KEY未設定でも200を返し、enabled=Falseで明示すること(30.8.4)。

    **2026-08-30 修正**:以前はこのテストが「開発者の `.env` に
    FRED_API_KEY が無いこと」に依存していた。実際に鍵を設定した日に
    このテストが落ちて発覚した——テストが環境の欠落を仕様として
    固定してしまっており、鍵を入れる作業を罰する形になっていた。
    未設定状態は環境ではなく明示的な差し替えで作る。
    """
    from autoscreener.api import routes as routes_module
    from autoscreener.config import Settings

    settings = Settings()
    monkeypatch.setattr(
        routes_module, "get_settings", lambda: settings.model_copy(update={"fred_api_key": None})
    )
    response = client.get("/api/v1/macro")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["series"] == []


def test_macro_endpoint_enabled_with_api_key(monkeypatch):
    """鍵があるときは enabled=True で系列を返すこと(観測が0行でも200)。"""
    from autoscreener.api import routes as routes_module
    from autoscreener.config import Settings

    settings = Settings()
    monkeypatch.setattr(
        routes_module, "get_settings", lambda: settings.model_copy(update={"fred_api_key": "dummy"})
    )
    response = client.get("/api/v1/macro")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert isinstance(body["series"], list)


# --- 30.7:保有・投資ノート・アラートAPI統合テスト -----------------------------

from autoscreener.db.models import Alert  # noqa: E402


def test_positions_endpoint_returns_empty_list_when_no_positions_file():
    """config/positions.yaml が存在しない状態でも200と空リストを返すこと(30.7.6)。"""
    response = client.get("/api/v1/positions")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["summary"]["position_count"] == 0
    # J-9:保有0件では portfolio 見通しは None、重複は空。
    assert body["portfolio"] is None
    assert body["ranking_overlap"] == []
    assert body["cash_ratio"] is None or 0.0 <= body["cash_ratio"] <= 1.0


# --- J-8(investment_decision_gap_2026-08-29.md):売却規律の器 ---


def test_exit_plan_view_picks_next_unreached_trim_and_matches_thesis_break():
    from types import SimpleNamespace

    from autoscreener.api.routes import _exit_plan_view

    note = SimpleNamespace(
        front_matter={
            "exit_plan": {
                "trim_rule": [
                    {"at_moic": 3.0, "action": "1/3 売却"},
                    {"at_moic": 6.0, "action": "さらに 1/3"},
                ],
                "thesis_break": [
                    {"condition": "粗利率低下", "indicator": "gross_margin_decline"},
                    {"condition": "希薄化", "indicator": "share_count_growth"},
                ],
            }
        }
    )
    # 達成倍率 3.2 → 次は 6.0
    next_trim, hits = _exit_plan_view(note, 3.2, {"gross_margin_decline"})
    assert next_trim.at_moic == 6.0
    assert next_trim.remaining_multiple == pytest.approx(2.8)
    assert hits == ["gross_margin_decline"]


def test_candidate_detail_supply_is_present_with_null_fields_when_no_data(seeded_candidate):
    body = client.get(f"/api/v1/candidates/{seeded_candidate}").json()
    assert body["supply"] is not None
    # 未取得は None(0 と区別する)
    assert body["supply"]["insider_net_shares_180d"] is None
    assert body["supply"]["short_interest_shares"] is None


def test_candidate_detail_supply_reports_short_lag_days():
    from autoscreener.dates import utc_today
    from autoscreener.db.models import InsiderTransaction, ShortInterest

    symbol = "ZZSUP2"
    _cleanup([symbol])
    today = utc_today()
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US", sector="Technology")
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id, snapshot_date=_TODAY, source="test",
                payload={"info": {"marketCap": 1_000_000_000}},
                content_hash="sup-h", last_seen_date=_TODAY, available_from=_TODAY, is_valid=True,
            )
        )
        session.add(UniverseSnapshot(snapshot_date=_TODAY, ticker_id=ticker.id, included=True, exclusion_reason=None))
        session.add(
            InsiderTransaction(
                ticker_id=ticker.id, accession_number="acc-1",
                transaction_date=today - datetime.timedelta(days=20),
                insider_name="Jane Doe", transaction_code="P", shares=5000,
            )
        )
        session.add(
            ShortInterest(
                ticker_id=ticker.id, settlement_date=today - datetime.timedelta(days=8),
                short_interest_shares=100000, days_to_cover=4.0, published_date=today,
            )
        )
    try:
        supply = client.get(f"/api/v1/candidates/{symbol}").json()["supply"]
        assert supply["insider_net_shares_180d"] == 5000
        assert supply["insider_buyer_count_180d"] == 1
        assert supply["short_interest_shares"] == 100000
        assert supply["short_lag_days"] == 8
    finally:
        with session_scope() as session:
            t = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if t is not None:
                session.query(InsiderTransaction).filter_by(ticker_id=t.id).delete()
                session.query(ShortInterest).filter_by(ticker_id=t.id).delete()
        _cleanup([symbol])


def test_calendar_endpoint_returns_upcoming_events():
    from autoscreener.dates import utc_today
    from autoscreener.db.models import EventCalendar

    symbol = "ZZCAL1"
    _cleanup([symbol])
    today = utc_today()
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US", sector="Technology")
        session.add(ticker)
        session.flush()
        session.add(
            EventCalendar(
                ticker_id=ticker.id,
                event_type="earnings",
                event_date=today + datetime.timedelta(days=10),
                is_estimated=True,
                source="yfinance",
                collected_on=today,
            )
        )
        session.add(
            EventCalendar(
                ticker_id=ticker.id,
                event_type="earnings",
                event_date=today + datetime.timedelta(days=200),  # 30日窓の外
                is_estimated=True,
                source="yfinance",
                collected_on=today,
            )
        )
    try:
        body = client.get("/api/v1/calendar?days=30").json()
        ours = [e for e in body["items"] if e["ticker"] == symbol]
        assert len(ours) == 1
        assert ours[0]["days_until"] == 10
        assert ours[0]["event_type"] == "earnings"
    finally:
        with session_scope() as session:
            t = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if t is not None:
                session.query(EventCalendar).filter_by(ticker_id=t.id).delete()
                session.delete(t)


def test_usdjpy_endpoint_prefers_fred_series_when_present():
    from autoscreener.db.models import MacroSeries

    with session_scope() as session:
        session.query(MacroSeries).filter_by(series_id="DEXJPUS").delete()
        session.add(
            MacroSeries(
                series_id="DEXJPUS",
                observation_date=datetime.date(2099, 6, 1),
                value=150.25,
            )
        )
    try:
        body = client.get("/api/v1/fx/usdjpy").json()
        assert body["rate"] == 150.25
        assert body["source"] == "fred:DEXJPUS"
        assert body["as_of"] == "2099-06-01"
    finally:
        with session_scope() as session:
            session.query(MacroSeries).filter_by(
                series_id="DEXJPUS", observation_date=datetime.date(2099, 6, 1)
            ).delete()


def test_exit_plan_view_handles_missing_note_and_missing_plan():
    from types import SimpleNamespace

    from autoscreener.api.routes import _exit_plan_view

    assert _exit_plan_view(None, 2.0, set()) == (None, [])
    empty = SimpleNamespace(front_matter={})
    assert _exit_plan_view(empty, 2.0, {"x"}) == (None, [])


def test_research_endpoint_returns_exists_false_for_ticker_without_note():
    response = client.get("/api/v1/research/ZZNONOTE")
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is False
    assert body["missing_fields"] == []


def test_alerts_endpoint_filters_by_severity_and_excludes_acknowledged(seeded_candidate):
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=seeded_candidate).one()
        session.add(
            Alert(
                ticker_id=ticker.id,
                code="cash_runway_low",
                severity="warning",
                source="metric",
                triggered_on=_TODAY,
                detail={"label": "test"},
            )
        )
        session.add(
            Alert(
                ticker_id=ticker.id,
                code="restatement",
                severity="blocking",
                source="red_flag",
                triggered_on=_TODAY,
                detail={"label": "test2"},
            )
        )
    try:
        blocking = client.get("/api/v1/alerts?days=365&severity=blocking").json()
        assert any(a["ticker"] == seeded_candidate and a["code"] == "restatement" for a in blocking["items"])
        assert not any(a["code"] == "cash_runway_low" for a in blocking["items"])

        all_open = client.get("/api/v1/alerts?days=365").json()
        assert sum(1 for a in all_open["items"] if a["ticker"] == seeded_candidate) == 2
    finally:
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=seeded_candidate).one()
            session.query(Alert).filter_by(ticker_id=ticker.id).delete()


def test_alerts_endpoint_rejects_unknown_severity():
    assert client.get("/api/v1/alerts?severity=bogus").status_code == 422


# --- J-1(investment_decision_gap_2026-08-29.md):会社概要の表示 ---


def _seed_profile_ticker(symbol: str, *, info: dict, listed_date=None, cik=None, industry=None) -> None:
    _cleanup([symbol])
    with session_scope() as session:
        ticker = Ticker(
            symbol=symbol,
            market="US",
            sector="Technology",
            industry=industry,
            listed_date=listed_date,
            cik=cik,
        )
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=_TODAY,
                source="test",
                payload={"info": info},
                content_hash=f"profile-hash-{symbol}",
                last_seen_date=_TODAY,
                available_from=_TODAY,
                is_valid=True,
            )
        )
        session.add(
            UniverseSnapshot(snapshot_date=_TODAY, ticker_id=ticker.id, included=True, exclusion_reason=None)
        )


def test_candidate_detail_returns_company_profile_from_info():
    symbol = "ZZPROF1"
    _seed_profile_ticker(
        symbol,
        info={
            "marketCap": 1_000_000_000,
            "longBusinessSummary": "ZZPROF1 designs and sells widgets to industrial customers.",
            "website": "https://zzprof1.example.com",
            "country": "United States",
            "fullTimeEmployees": 1234,
            "exchange": "NMS",
        },
        listed_date=datetime.date(2015, 6, 1),
        cik="0000123456",
        industry="Widgets",
    )
    try:
        body = client.get(f"/api/v1/candidates/{symbol}").json()
        profile = body["profile"]
        assert profile is not None
        assert profile["business_summary"].startswith("ZZPROF1 designs")
        assert profile["website"] == "https://zzprof1.example.com"
        assert profile["full_time_employees"] == 1234
        assert profile["industry"] == "Widgets"
        assert profile["listed_date"] == "2015-06-01"
        assert profile["cik"] == "0000123456"
        assert profile["profile_as_of"] == _TODAY.isoformat()
    finally:
        _cleanup([symbol])


def test_candidate_detail_returns_null_profile_when_info_empty_but_still_200():
    symbol = "ZZPROF2"
    _seed_profile_ticker(symbol, info={})
    try:
        response = client.get(f"/api/v1/candidates/{symbol}")
        assert response.status_code == 200
        assert response.json()["profile"] is None
    finally:
        _cleanup([symbol])


def test_candidate_detail_profile_falls_back_to_ticker_columns_when_info_sparse():
    symbol = "ZZPROF3"
    _seed_profile_ticker(
        symbol,
        info={"marketCap": 500_000_000},
        listed_date=datetime.date(2020, 1, 15),
        cik="0000999999",
    )
    try:
        profile = client.get(f"/api/v1/candidates/{symbol}").json()["profile"]
        assert profile is not None
        assert profile["business_summary"] is None
        assert profile["listed_date"] == "2020-01-15"
        assert profile["cik"] == "0000999999"
    finally:
        _cleanup([symbol])


# --- J-2(investment_decision_gap_2026-08-29.md):財務推移エンドポイント ---


def test_financials_endpoint_returns_series_from_payload():
    symbol = "ZZFIN1"
    _seed_profile_ticker(
        symbol,
        info={"currency": "USD", "financialCurrency": "USD", "marketCap": 1_000_000_000},
    )
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=symbol).one()
        raw = session.query(RawSnapshot).filter_by(ticker_id=ticker.id).one()
        raw.payload = {
            "info": raw.payload["info"],
            "income_stmt": {
                "Total Revenue": {
                    "2022-12-31": 150.0,
                    "2023-12-31": 210.0,
                    "2024-12-31": 300.0,
                },
                "Gross Profit": {
                    "2022-12-31": 66.0,
                    "2023-12-31": 100.0,
                    "2024-12-31": 150.0,
                },
            },
            "balance_sheet": {
                "Total Debt": {"2023-12-31": 50.0, "2024-12-31": 40.0},
                "Cash And Cash Equivalents": {"2023-12-31": 120.0, "2024-12-31": 90.0},
                "Ordinary Shares Number": {"2023-12-31": 12.0, "2024-12-31": 13.0},
            },
            "cash_flow": {"Free Cash Flow": {"2023-12-31": -40.0, "2024-12-31": -30.0}},
            "quarterly_cash_flow": {
                "Free Cash Flow": {
                    "2024-06-30": -9.0,
                    "2024-09-30": -7.0,
                    "2024-12-31": -6.0,
                }
            },
            "quarterly_balance_sheet": {"Cash And Cash Equivalents": {"2024-12-31": 90.0}},
        }
    try:
        body = client.get(f"/api/v1/candidates/{symbol}/financials").json()
        assert body["ticker"] == symbol
        assert [p["revenue"] for p in body["annual"]] == [150.0, 210.0, 300.0]
        assert body["annual"][-1]["gross_margin"] == 0.5
        assert body["annual"][-1]["net_debt"] == -50.0
        assert body["derived"]["revenue_yoy"] == 300.0 / 210.0 - 1
        assert body["derived"]["runway_months"] is not None
        assert len(body["derived"]["piotroski_criteria"]) == 9
        assert body["as_of"] == "2024-12-31"
    finally:
        _cleanup([symbol])


def test_financials_endpoint_returns_200_with_empty_series_for_payload_without_statements():
    symbol = "ZZFIN2"
    _seed_profile_ticker(symbol, info={"marketCap": 1_000_000_000})
    try:
        response = client.get(f"/api/v1/candidates/{symbol}/financials")
        assert response.status_code == 200
        body = response.json()
        assert body["annual"] == []
        assert body["quarterly"] == []
        assert body["as_of"] is None
    finally:
        _cleanup([symbol])


def test_financials_endpoint_unknown_ticker_returns_404():
    assert client.get("/api/v1/candidates/ZZNOTREAL8/financials").status_code == 404


# --- J-3(investment_decision_gap_2026-08-29.md):バリュエーションの現在地 ---


def test_candidate_detail_reports_52_week_range_and_ev_history():
    from autoscreener.dates import utc_today
    from autoscreener.db.models import PriceSnapshot

    symbol = "ZZVAL1"
    _cleanup([symbol])
    _cleanup_filings_and_prices([symbol])
    today = utc_today()
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US", sector="Technology")
        session.add(ticker)
        session.flush()
        session.add(
            RawSnapshot(
                ticker_id=ticker.id,
                snapshot_date=_TODAY,
                source="test",
                payload={"info": {"marketCap": 1_000_000_000, "currentPrice": 30.0}},
                content_hash="val-hash-1",
                last_seen_date=_TODAY,
                available_from=_TODAY,
                is_valid=True,
            )
        )
        session.add(
            UniverseSnapshot(snapshot_date=_TODAY, ticker_id=ticker.id, included=True, exclusion_reason=None)
        )
        session.add(
            Score(
                ticker_id=ticker.id,
                score_date=_TODAY,
                scoring_version=_SCORING_VERSION,
                config_hash="test-config-hash",
                probability=0.05,
                median_moic=3.0,
                log_moic_mu=1.1,
                log_moic_sigma=0.9,
                survival_probability=0.7,
                factors={
                    "expected_moic": 4.0,
                    "current_ev_to_gross_profit": 12.4,
                    "ev_to_gross_profit_percentile_universe": 0.8,
                },
            )
        )
        for i, close in enumerate([10.0, 40.0, 30.0]):
            session.add(
                PriceSnapshot(
                    ticker_id=ticker.id,
                    trade_date=today - datetime.timedelta(days=30 * (2 - i)),
                    close=close,
                    volume=1000,
                )
            )
    try:
        body = client.get(f"/api/v1/candidates/{symbol}").json()
        assert body["week52_high"] == 40.0
        assert body["week52_low"] == 10.0
        # current close 30 → (30-10)/(40-10) = 0.666...
        assert abs(body["week52_position"] - 2 / 3) < 1e-9
        assert body["factors"]["ev_to_gross_profit_percentile_universe"] == 0.8
        hist = body["score_history"][0]
        assert hist["ev_to_gross_profit"] == 12.4
    finally:
        _cleanup_filings_and_prices([symbol])
        _cleanup([symbol])


# --- J-4(investment_decision_gap_2026-08-29.md):実現倍率の分位点 ---


def test_candidate_detail_and_list_expose_moic_quantiles(seeded_candidate):
    detail = client.get(f"/api/v1/candidates/{seeded_candidate}").json()
    q = detail["moic_quantiles"]
    assert q is not None
    assert set(q) == {"p10", "p25", "p50", "p75", "p90"}
    # 単調増加
    assert q["p10"] <= q["p25"] <= q["p50"] <= q["p75"] <= q["p90"]
    # survival 0.71 → 1-S = 0.29 なので P10/P25 は 0.0(混合分布)
    assert q["p10"] == 0.0

    body = client.get(f"/api/v1/candidates?date={_TODAY.isoformat()}").json()
    item = next(i for i in body["items"] if i["ticker"] == seeded_candidate)
    assert item["moic_p10"] == q["p10"]
    assert item["moic_p90"] == q["p90"]
