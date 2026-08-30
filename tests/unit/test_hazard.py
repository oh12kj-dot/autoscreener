"""上場廃止ハザード較正のテスト(defect_and_edge_audit_2026-08-28.md D-9)。純粋関数。"""

import math
import random

import pytest

from autoscreener.scoring.hazard import estimate_hazard, fit_logistic


def test_none_without_events():
    """廃止が1件も無ければ推定不能(D-1 未完了の状態)。"""
    assert estimate_hazard([(0.1, False)] * 50) is None


def test_none_with_too_few_observations():
    assert estimate_hazard([(0.0, True), (0.1, False)]) is None


def test_recovers_known_logistic_parameters():
    rng = random.Random(0)
    true_a, true_b = -2.0, -1.5  # 健全(health高)ほど廃止しにくい
    xs, ys = [], []
    for _ in range(4000):
        x = rng.uniform(-1.0, 1.0)
        p = 1.0 / (1.0 + math.exp(-(true_a + true_b * x)))
        xs.append(x)
        ys.append(1.0 if rng.random() < p else 0.0)
    a, b, converged = fit_logistic(xs, ys)
    assert converged
    assert a == pytest.approx(true_a, abs=0.2)
    assert b == pytest.approx(true_b, abs=0.3)


def test_estimate_hazard_reports_base_rate_and_sensitivity():
    rng = random.Random(1)
    true_a, true_b = -2.5, -1.2
    obs = []
    for _ in range(5000):
        x = rng.uniform(-1.0, 1.0)
        p = 1.0 / (1.0 + math.exp(-(true_a + true_b * x)))
        obs.append((x, rng.random() < p))
    est = estimate_hazard(obs)
    assert est is not None
    assert est.n_events > 0
    # base_annual_hazard = sigmoid(a) ~ sigmoid(-2.5) ~ 0.076
    assert est.base_annual_hazard == pytest.approx(1 / (1 + math.exp(2.5)), abs=0.03)
    # 健全ほど廃止しにくい -> health_sensitivity は正
    assert est.health_sensitivity > 0
