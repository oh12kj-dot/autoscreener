"""バックテストKPI算出のテスト(14.2・27.12)。"""

import math

import pytest

from autoscreener.backtest.metrics import (
    Observation,
    compute_metrics,
    on_pace_threshold,
    per_date_stats,
    scale_probability_to_horizon,
    spearman,
    tail_lifts,
)


def make_observation(
    probability: float,
    realized_return: float,
    settlement: str = "market",
    base_date: str = "2025-01-01",
    growth_nowcast_adjustment: float = 0.0,
    ticker_id: int = 1,
) -> Observation:
    # mu/sigma は較正指標にしか使わないので、確率と整合する適当な値を入れる
    return Observation(
        ticker_id=ticker_id,
        base_date=base_date,
        probability=probability,
        log_moic_mu=math.log(max(probability, 1e-6) * 20 + 1),
        log_moic_sigma=1.0,
        realized_return=realized_return,
        settlement=settlement,
        growth_nowcast_adjustment=growth_nowcast_adjustment,
    )


# --- オンペース閾値 -----------------------------------------------------------


def test_on_pace_threshold_matches_the_ten_bagger_annual_rate():
    """1年での等価閾値は 10^(1/7) = 1.389倍(=年率38.9%)。"""
    assert on_pace_threshold(10.0, 1.0, 7.0) == pytest.approx(1.389, rel=1e-3)


def test_on_pace_threshold_at_the_full_horizon_is_the_target_itself():
    assert on_pace_threshold(10.0, 7.0, 7.0) == pytest.approx(10.0)


def test_scaled_probability_is_below_one_and_above_zero():
    p = scale_probability_to_horizon(log_moic_mu=1.0, log_moic_sigma=1.0, target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0)
    assert 0.0 < p < 1.0


def test_scaled_probability_rises_with_the_projected_multiple():
    low = scale_probability_to_horizon(0.2, 1.0, 10.0, 1.0, 7.0)
    high = scale_probability_to_horizon(2.0, 1.0, 10.0, 1.0, 7.0)
    assert high > low


# --- 順位相関 -----------------------------------------------------------------


def test_spearman_detects_perfect_monotonicity():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_is_zero_for_degenerate_input():
    assert spearman([1, 2], [1, 2]) == 0.0
    assert spearman([1, 2, 3], [5, 5, 5]) == 0.0


# --- KPI ----------------------------------------------------------------------


def test_empty_observations_produce_a_zeroed_report():
    metrics = compute_metrics([], target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0)
    assert metrics.observation_count == 0
    assert metrics.deciles == []
    assert metrics.lift_ratio == 0.0


def test_deciles_are_ordered_by_probability_descending():
    """デシル1はモデルが最も有望と判定した10%でなければならない。"""
    observations = [make_observation(p / 100, 0.0) for p in range(1, 101)]
    metrics = compute_metrics(observations, 10.0, 1.0, 7.0, decile_count=10)
    means = [d.mean_probability for d in metrics.deciles]
    assert means == sorted(means, reverse=True)


def test_perfectly_predictive_model_is_monotonic_with_high_lift():
    """スコアとリターンが完全に一致する人工データで、KPIが最良値になること。"""
    observations = [make_observation(p / 100, p / 100) for p in range(1, 101)]
    metrics = compute_metrics(observations, 10.0, 1.0, 7.0, decile_count=10)
    assert metrics.decile_monotonicity == pytest.approx(1.0)
    assert metrics.strictly_monotonic is True
    assert metrics.lift_ratio > 1.0


def test_useless_model_has_no_monotonicity():
    """スコアとリターンが無関係なら単調性はほぼ0、リフトはほぼ1になる。"""
    observations = [make_observation(p / 100, 0.10 if p % 2 else -0.10) for p in range(1, 101)]
    metrics = compute_metrics(observations, 10.0, 1.0, 7.0, decile_count=10)
    assert abs(metrics.decile_monotonicity) < 0.5
    assert metrics.lift_ratio == pytest.approx(0.0)  # 誰もオンペースに届かない


def test_lift_ratio_compares_top_decile_to_the_universe():
    # 上位10件だけがオンペース(+50% > 1.389倍には届かないので +60% を使う)
    winners = [make_observation(0.9 - i * 0.001, 0.60) for i in range(10)]
    losers = [make_observation(0.1 - i * 0.0001, -0.10) for i in range(90)]
    metrics = compute_metrics(winners + losers, 10.0, 1.0, 7.0, decile_count=10)
    assert metrics.universe_on_pace_rate == pytest.approx(0.10)
    assert metrics.deciles[0].on_pace_rate == pytest.approx(1.0)
    assert metrics.lift_ratio == pytest.approx(10.0)


def test_loss_rate_counts_severe_drawdowns():
    observations = [make_observation(0.5, -0.6)] * 2 + [make_observation(0.4, 0.1)] * 2
    metrics = compute_metrics(observations, 10.0, 1.0, 7.0, decile_count=2)
    assert metrics.universe_loss_rate == pytest.approx(0.5)


def test_delisted_settlement_rate_is_reported():
    """27.15:この値が0%なら「廃止が起きなかった」ではなく「標本に無い」を疑う。"""
    observations = [
        make_observation(0.5, -0.95, settlement="delisted"),
        make_observation(0.4, 0.1),
        make_observation(0.3, 0.2),
        make_observation(0.2, 0.3),
    ]
    metrics = compute_metrics(observations, 10.0, 1.0, 7.0, decile_count=4)
    assert metrics.delisted_settlement_rate == pytest.approx(0.25)


def test_calibration_error_is_prediction_minus_reality():
    observations = [make_observation(0.5, 1.0) for _ in range(10)]  # 全員オンペース
    metrics = compute_metrics(observations, 10.0, 1.0, 7.0, decile_count=2)
    assert metrics.universe_on_pace_rate == pytest.approx(1.0)
    assert metrics.calibration_error == pytest.approx(
        metrics.mean_predicted_on_pace_rate - 1.0
    )


# --- 右裾のリフト(28.11)------------------------------------------------------


def _perfect_ranking(base_date: str, count: int = 200) -> list[Observation]:
    """確率の順位と実現リターンの順位が完全に一致する観測群。"""
    return [
        make_observation(probability=(count - i) / count, realized_return=(count - i) / 20, base_date=base_date)
        for i in range(count)
    ]


def _random_ranking(base_date: str, count: int = 200) -> list[Observation]:
    """確率と実現リターンが無関係な観測群(リフトは1に近くなるはず)。"""
    return [
        make_observation(
            probability=(count - i) / count,
            realized_return=((i * 37) % count) / 20,
            base_date=base_date,
        )
        for i in range(count)
    ]


def test_tail_lift_threshold_is_a_cross_sectional_quantile():
    """**閾値は絶対値ではなくその日の断面の分位**(28.11)。

    絶対閾値だと、強気相場の評価日では基準率が45%、弱気相場では17%になり、
    リフトの比較がレジームの比較になってしまう。分位で切れば基準率は定義上
    一定なので、残るのは「モデルが勝ち組を引けたか」だけになる。
    """
    bull = [
        make_observation(probability=(50 - i) / 50, realized_return=1.0 + i / 50, base_date="2025-01-01")
        for i in range(50)
    ]
    bear = [
        make_observation(probability=(50 - i) / 50, realized_return=-0.5 + i / 50, base_date="2025-01-01")
        for i in range(50)
    ]
    # リターンの水準はまったく違うが、順位構造は同じなのでリフトも同じになる
    bull_lift = tail_lifts(bull, quantiles=(0.10,))[0]
    bear_lift = tail_lifts(bear, quantiles=(0.10,))[0]
    assert bull_lift.lift == pytest.approx(bear_lift.lift)


def test_tail_lift_is_maximal_for_a_perfect_ranking():
    """完全な順位なら、上位10%は上位10%の事象をすべて捕まえる = リフト10倍。"""
    result = tail_lifts(_perfect_ranking("2025-01-01"), quantiles=(0.10,))[0]
    assert result.lift == pytest.approx(10.0, rel=0.05)
    assert result.top_decile_hit_rate == pytest.approx(1.0, rel=0.05)


def test_tail_lift_is_about_one_for_an_uninformative_ranking():
    result = tail_lifts(_random_ranking("2025-01-01"), quantiles=(0.10,))[0]
    assert 0.5 < result.lift < 1.6


def test_tail_lift_reports_the_worst_date_not_only_the_average():
    """平均だけを見ると「9日中3日で効いていない」が消える(28.9・28.11)。"""
    observations = _perfect_ranking("2025-01-01") + _random_ranking("2025-04-01")
    result = tail_lifts(observations, quantiles=(0.10,))[0]
    assert result.worst_date_lift < result.lift


# --- 評価日ごとの内訳(28.9)---------------------------------------------------


def test_per_date_stats_split_by_evaluation_date():
    observations = _perfect_ranking("2025-01-01") + _random_ranking("2025-04-01")
    stats = per_date_stats(observations, threshold=1.389)
    assert [s.base_date for s in stats] == ["2025-01-01", "2025-04-01"]
    assert stats[0].rank_ic > stats[1].rank_ic
    assert all(s.count == 200 for s in stats)


def test_per_date_stats_skip_thin_dates():
    """観測が数件しかない評価日を混ぜると、比率が二項ノイズそのものになる。"""
    observations = _perfect_ranking("2025-01-01") + [
        make_observation(0.5, 0.1, base_date="2025-04-01") for _ in range(5)
    ]
    stats = per_date_stats(observations, threshold=1.389)
    assert [s.base_date for s in stats] == ["2025-01-01"]


def test_metrics_expose_the_worst_date_lift():
    observations = _perfect_ranking("2025-01-01") + _random_ranking("2025-04-01")
    metrics = compute_metrics(observations, target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0)
    assert metrics.lift_ratio_worst_date <= metrics.lift_ratio
    assert len(metrics.per_date) == 2
    assert len(metrics.tail_lifts) == 4
    assert metrics.rank_ic > 0


# --- ナウキャスト上限への張り付き率(S-8、model_audit_v4_2026-08-26.md) --------


def test_nowcast_cap_hit_rate_counts_observations_pinned_at_the_cap():
    observations = [
        make_observation(0.02, 0.1, growth_nowcast_adjustment=0.15),  # 上限に張り付き
        make_observation(0.02, 0.1, growth_nowcast_adjustment=-0.15),  # 反対方向でも張り付き扱い
        make_observation(0.02, 0.1, growth_nowcast_adjustment=0.05),  # 張り付いていない
        make_observation(0.02, 0.1, growth_nowcast_adjustment=0.0),
    ]
    metrics = compute_metrics(
        observations, target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0, nowcast_cap=0.15
    )
    assert metrics.nowcast_cap_hit_rate == pytest.approx(0.5)


def test_nowcast_cap_hit_rate_is_zero_when_cap_not_supplied():
    """既存の呼び出し元(nowcast_cap省略)を壊さないための後方互換テスト。"""
    observations = [make_observation(0.02, 0.1, growth_nowcast_adjustment=0.15)]
    metrics = compute_metrics(observations, target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0)
    assert metrics.nowcast_cap_hit_rate == 0.0


# --- デシルを評価日ごとに切る(2026-08-26修正) --------------------------------


def test_deciles_are_formed_within_each_evaluation_date():
    """評価日をまたいでプールしたまま10等分すると、上位デシルが「モデルが有望と
    見た銘柄」ではなく「**確率の水準が高かった評価日**の銘柄」で埋まる。

    モデルが出す確率の水準は、断面統計(σ の縮小中心・ナウキャストの基準線)が
    その日のユニバースから作られるため評価日ごとに動く。実データでも評価日ごとの
    ユニバース・オンペース率は 16.0% 〜 35.1% と2倍以上ばらついていた。

    ここでは極端な形で再現する:A日は確率が高いが**モデルの序列は逆**、B日は
    確率が低いが序列は正しい。プール方式だとA日の全銘柄が上位デシルを占め、
    単調性が負に出る。評価日ごとに切れば、両日の上位10%が正しく混ざる。
    """
    observations = []
    # A日:確率は 0.50〜0.41 と高いが、確率が高い銘柄ほどリターンが低い
    for i in range(100):
        observations.append(
            make_observation(0.50 - i * 0.001, realized_return=i * 0.01, base_date="2025-01-01")
        )
    # B日:確率は 0.10〜0.01 と低いが、確率が高い銘柄ほどリターンが高い
    for i in range(100):
        observations.append(
            make_observation(0.10 - i * 0.001, realized_return=1.0 - i * 0.01, base_date="2025-04-01")
        )

    metrics = compute_metrics(observations, target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0)

    # 各デシルに両方の評価日から同数ずつ入る(日ごとに10等分して束ねるため)
    assert [d.count for d in metrics.deciles] == [20] * 10
    # プール方式では上位デシルがA日だけで埋まり単調性が −1 になっていた。
    # 日ごとに切れば、逆向きの2日が打ち消し合って極端な値にはならない。
    assert metrics.decile_monotonicity > -0.5


def test_tied_probabilities_bucket_deterministically_regardless_of_input_order():
    """E-8: 確率が完全同値の観測は、入力順(dictの挿入順)に依存せず、
    ticker_id をタイブレークキーとして決定的にデシルへ割り当てられること。"""
    from autoscreener.backtest.metrics import _cross_sectional_buckets

    # 全銘柄が同一確率。ticker_id だけが異なる。
    forward = [
        make_observation(0.05, realized_return=0.0, base_date="2025-01-01", ticker_id=tid)
        for tid in range(100)
    ]
    reverse = list(reversed(forward))

    buckets_forward = _cross_sectional_buckets(forward, bucket_count=10)
    buckets_reverse = _cross_sectional_buckets(reverse, bucket_count=10)

    ids_forward = [[o.ticker_id for o in b] for b in buckets_forward]
    ids_reverse = [[o.ticker_id for o in b] for b in buckets_reverse]
    assert ids_forward == ids_reverse
    # 先頭バケットは最小の ticker_id 群(昇順タイブレーク)
    assert ids_forward[0] == list(range(10))


def test_decile_bucketing_is_unchanged_for_a_single_evaluation_date():
    """評価日が1つしかない場合は従来どおり(挙動を変えていないことの確認)。"""
    observations = [
        make_observation(1.0 - i * 0.005, realized_return=1.0 - i * 0.01, base_date="2025-01-01")
        for i in range(100)
    ]
    metrics = compute_metrics(observations, target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0)
    assert [d.count for d in metrics.deciles] == [10] * 10
    assert metrics.decile_monotonicity == pytest.approx(1.0)
    assert metrics.strictly_monotonic is True
