"""`scores` の書き込みが全カラムを網羅していることのテスト(28.19)。

**なぜこのテストが要るか。** スコアリングは同一 (銘柄, 日付, バージョン) の行を
`setattr` で上書きするため、書き込む値の辞書に無いカラムは**前回実行の値が
そのまま残る**。エラーも警告も出ない。

実際に `calibrated_on_pace_probability` の記載漏れで、ランキング対象から外れた
14銘柄に「以前ランキングされていたときの1年オンペース率」が残り続けていた。
順位が付かない銘柄の詳細画面に較正済み確率が出るという、27.20 が防ごうとして
いた種類の誤情報そのものである。
"""

import pytest
from sqlalchemy import inspect

from autoscreener.db.models import Score
from autoscreener.scoring.engine import _MUTABLE_SCORE_COLUMNS, _score_values
from autoscreener.scoring.moic import MoicResult

# スコアリングが書き換えないカラム。主キー・行の同一性を決める列・DB既定値。
_IDENTITY_COLUMNS = {"id", "ticker_id", "score_date", "scoring_version", "created_at"}


def make_result(**overrides) -> MoicResult:
    base = dict(
        probability=0.03,
        expected_moic=3.0,
        median_moic=2.4,
        log_moic_mu=0.87,
        log_moic_sigma=0.95,
        survival_probability=0.65,
        size_prior=1.0,
        revenue_multiple=2.5,
        margin_multiple=1.0,
        multiple_change=0.9,
        leverage_effect=1.0,
        dilution_drag=1.05,
        initial_growth_rate=0.3,
        base_growth_rate=0.28,
        growth_nowcast_adjustment=0.02,
        terminal_growth_rate=0.06,
        growth_fade_rate=0.77,
        terminal_gross_margin=0.5,
        current_ev_to_gross_profit=5.0,
        target_ev_to_gross_profit=4.5,
        implied_terminal_ev=1.0e9,
        health_index=0.3,
        raw_log_moic_sigma=1.1,
    )
    base.update(overrides)
    return MoicResult(**base)


def test_mutable_columns_cover_every_writable_column_of_the_model():
    """**モデルにカラムを足したのに書き忘れる**、という事故をここで止める。

    `_MUTABLE_SCORE_COLUMNS` が実際のテーブル定義と食い違っていれば、
    その差分がそのまま「更新されずに古い値が残るカラム」になる。
    """
    model_columns = {c.key for c in inspect(Score).mapper.column_attrs}
    writable = model_columns - _IDENTITY_COLUMNS
    assert writable == set(_MUTABLE_SCORE_COLUMNS), (
        "scores のカラムと書き込み対象が食い違っています。"
        f"差分: {writable ^ set(_MUTABLE_SCORE_COLUMNS)}"
    )


def test_ranked_and_unranked_write_the_same_columns():
    """順位あり・なしで書き込む**カラム集合**が同じであること。

    片方だけが一部のカラムを省略すると、状態が遷移したときに古い値が残る。
    """
    ranked = _score_values(
        "hash", {"a": 1}, make_result(), probability=0.03, calibrated=0.31, unranked_reason=None
    )
    unranked = _score_values(
        "hash",
        {"a": 1},
        make_result(expected_moic=0.7),
        probability=None,
        calibrated=None,
        unranked_reason="negative_outlook",
    )
    assert set(ranked) == set(unranked) == set(_MUTABLE_SCORE_COLUMNS)


def test_unranked_rows_clear_the_calibrated_probability():
    """実際に踏んだバグのリグレッションテスト(28.19)。

    確率が無い以上、それを較正した値も存在しえない。前回ランキングされていた
    ときの値が残っていると、順位の付かない銘柄に「1年オンペース率 32%」が
    表示される。
    """
    unranked = _score_values(
        "hash",
        {"a": 1},
        make_result(expected_moic=0.7),
        probability=None,
        calibrated=None,
        unranked_reason="negative_outlook",
    )
    assert unranked["probability"] is None
    assert unranked["calibrated_on_pace_probability"] is None
    assert unranked["factors"]["unranked_reason"] == "negative_outlook"


def test_ranked_rows_carry_no_unranked_reason():
    """順位が付いた銘柄に `unranked_reason` が紛れ込まないこと。"""
    ranked = _score_values(
        "hash", {"a": 1}, make_result(), probability=0.03, calibrated=0.31, unranked_reason=None
    )
    assert "unranked_reason" not in ranked["factors"]
    assert ranked["probability"] == pytest.approx(0.03)
    assert ranked["calibrated_on_pace_probability"] == pytest.approx(0.31)
