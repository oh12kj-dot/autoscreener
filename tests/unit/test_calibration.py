"""確率の較正層のテスト(28.8)。"""

import pytest

from autoscreener.scoring.calibration import (
    CalibrationMap,
    CalibrationPoint,
    fit_calibration,
    isotonic,
)


# --- 等調回帰 -----------------------------------------------------------------


def test_isotonic_leaves_an_already_monotone_series_alone():
    pairs = [(0.1, 0.05), (0.2, 0.10), (0.3, 0.30)]
    assert isotonic(pairs) == pairs


def test_isotonic_pools_violations_into_their_average():
    """単調性が破れた隣接ブロックだけが平均に潰される。

    実測頻度は標本ノイズで簡単に逆転するが、その逆転を残すと「予測が高いほど
    実測が低い」という較正写像ができてしまい、確率の意味が壊れる。
    """
    smoothed = isotonic([(0.1, 0.10), (0.2, 0.40), (0.3, 0.20), (0.4, 0.50)])
    values = [y for _, y in smoothed]
    assert values == sorted(values)
    # 0.40 と 0.20 が併合されて 0.30 になる
    assert pytest.approx(0.30) in values


def test_isotonic_handles_a_fully_reversed_series():
    """完全に逆順なら全体が1つの平均に潰れる(=較正が何も主張しない状態)。"""
    smoothed = isotonic([(0.1, 0.9), (0.2, 0.6), (0.3, 0.3)])
    assert len(smoothed) == 1
    assert smoothed[0][1] == pytest.approx(0.6)


# --- 写像の適用 ---------------------------------------------------------------


def test_apply_interpolates_between_points():
    calibration = CalibrationMap(
        horizon_days=365,
        observation_count=3000,
        points=[CalibrationPoint(0.10, 0.20), CalibrationPoint(0.30, 0.40)],
    )
    assert calibration.apply(0.20) == pytest.approx(0.30)


def test_apply_never_extrapolates_outside_the_observed_range():
    """**観測していない確率帯について実測頻度を主張しない**(28.8)。

    外挿を許すと、バックテストが一度も見ていない予測60%の銘柄に対して
    「実測では80%です」といった、何の裏付けもない数字を出すことになる。
    """
    calibration = CalibrationMap(
        horizon_days=365,
        observation_count=3000,
        points=[CalibrationPoint(0.10, 0.20), CalibrationPoint(0.30, 0.40)],
    )
    assert calibration.apply(0.001) == pytest.approx(0.20)
    assert calibration.apply(0.99) == pytest.approx(0.40)


def test_apply_is_monotone_so_ranking_is_unchanged():
    """較正は**順位を変えない**。変わるのは確率の水準だけ(28.8)。"""
    calibration = CalibrationMap(
        horizon_days=365,
        observation_count=3000,
        points=[CalibrationPoint(0.02, 0.05), CalibrationPoint(0.10, 0.22), CalibrationPoint(0.30, 0.45)],
    )
    raw = [0.01, 0.03, 0.08, 0.15, 0.28, 0.50]
    calibrated = [calibration.apply(p) for p in raw]
    assert calibrated == sorted(calibrated)


def test_map_round_trips_through_json():
    original = CalibrationMap(
        horizon_days=365,
        observation_count=3000,
        points=[CalibrationPoint(0.02, 0.05), CalibrationPoint(0.10, 0.22)],
        fitted_at="2026-08-26T00:00:00+00:00",
    )
    assert CalibrationMap.from_dict(original.to_dict()) == original
    assert CalibrationMap.from_dict(None) is None
    assert CalibrationMap.from_dict({"points": []}) is None


# --- 学習 ---------------------------------------------------------------------


def test_fit_refuses_to_calibrate_on_too_few_observations():
    """**足りない標本で較正すると、較正そのものがノイズを固定する**(28.8)。

    較正しなければ「モデルの対数正規仮定どおり」という既知の状態に留まれる。
    どちらも正しくはないが、後者のほうが誤りの性質が分かっている。
    """
    predicted = [i / 100 for i in range(100)]
    outcomes = [i % 3 == 0 for i in range(100)]
    assert fit_calibration(predicted, outcomes, horizon_days=365, min_observations=1000) is None


def test_fit_recovers_a_systematic_optimism():
    """モデルが一律に強気なとき、較正写像は水準を引き下げること。"""
    # 予測は 0〜40% に散らばるが、実際に当たるのはその半分の頻度
    predicted, outcomes = [], []
    for i in range(2000):
        p = (i % 40) / 100
        predicted.append(p)
        # 実測頻度が予測のおよそ半分になるよう決定的に配置する
        outcomes.append((i // 40) < (p / 2) * 50)
    calibration = fit_calibration(predicted, outcomes, horizon_days=365, min_observations=500)
    assert calibration is not None
    assert calibration.horizon_days == 365
    assert calibration.observation_count == 2000
    # 高い予測帯ほど較正値も高い(単調性は保たれる)
    values = [p.calibrated for p in calibration.points]
    assert values == sorted(values)
    # 一番高い予測帯でも、予測の値そのものより低い水準に落ちている
    top = calibration.points[-1]
    assert top.calibrated < top.predicted
