import pandas as pd
import pytest

from autoscreener.collectors.yfinance_client import _apply_split_adjustment


def test_split_adjustment_scales_pre_split_observations():
    # CELHの実際のケースを模した合成データ:2023-11-15に3:1分割
    shares = pd.Series(
        {
            pd.Timestamp("2023-08-22"): 76_000_000,
            pd.Timestamp("2023-11-01"): 76_500_000,  # 分割前
            pd.Timestamp("2023-12-01"): 231_000_000,  # 分割後
            pd.Timestamp("2024-06-01"): 240_000_000,  # 分割後
        }
    )
    splits = pd.Series({pd.Timestamp("2023-11-15"): 3.0})

    adjusted = _apply_split_adjustment(shares, splits)

    # 分割前の観測値は3倍される
    assert adjusted[pd.Timestamp("2023-08-22")] == 76_000_000 * 3
    assert adjusted[pd.Timestamp("2023-11-01")] == 76_500_000 * 3
    # 分割後の観測値は変化しない
    assert adjusted[pd.Timestamp("2023-12-01")] == 231_000_000
    assert adjusted[pd.Timestamp("2024-06-01")] == 240_000_000


def test_split_adjustment_no_splits_is_noop():
    shares = pd.Series({pd.Timestamp("2024-01-01"): 100_000_000})
    adjusted = _apply_split_adjustment(shares, pd.Series(dtype=float))
    assert adjusted[pd.Timestamp("2024-01-01")] == 100_000_000


def test_split_adjustment_none_splits_is_noop():
    shares = pd.Series({pd.Timestamp("2024-01-01"): 100_000_000})
    adjusted = _apply_split_adjustment(shares, None)
    assert adjusted[pd.Timestamp("2024-01-01")] == 100_000_000


def test_split_adjustment_handles_intraday_timestamp_mismatch():
    # 実データ回帰テスト:ticker.splits は寄り付き時刻(09:30:00)付き、
    # get_shares_full は日付のみ(00:00:00)。CELHの実データで確認したところ、
    # 分割「当日」のraw観測値は既に分割後の値だった。単純な `index > ts` 比較
    # (時刻付きのまま)だと、当日の観測値まで誤って3倍してしまう
    # (231.7M→695M という誤り。修正前に実際に発生した)。
    shares = pd.Series(
        {
            pd.Timestamp("2023-11-14 00:00:00", tz="America/New_York"): 77_225_000,  # 分割前日(raw、分割前単位)
            pd.Timestamp("2023-11-15 00:00:00", tz="America/New_York"): 231_675_008,  # 分割当日(raw、既に分割後単位)
            pd.Timestamp("2023-11-16 00:00:00", tz="America/New_York"): 231_675_008,  # 翌日
        }
    )
    splits = pd.Series({pd.Timestamp("2023-11-15 09:30:00", tz="America/New_York"): 3.0})

    adjusted = _apply_split_adjustment(shares, splits)

    # 分割前日は分割後単位に引き上げられる
    assert adjusted[pd.Timestamp("2023-11-14 00:00:00", tz="America/New_York")] == 77_225_000 * 3
    # 分割当日・翌日は既に分割後単位なので変化しない(ここが修正前は695Mになっていた)
    assert adjusted[pd.Timestamp("2023-11-15 00:00:00", tz="America/New_York")] == 231_675_008
    assert adjusted[pd.Timestamp("2023-11-16 00:00:00", tz="America/New_York")] == 231_675_008


def test_split_adjustment_fractional_factor_does_not_raise():
    # 実データ回帰テスト(2026-08-24、ABTC/ADV/ACB等680銘柄で発生):分割倍率が
    # 整数倍にならない場合(変則的な比率・複数分割の複合)、株数の元Seriesが
    # int64のままだと端数を代入しようとしてpandas.errors.LossySetitemErrorに
    # 起因するTypeErrorが送出され、その銘柄の価格・株式数履歴が丸ごと取得できなく
    # なるバグがあった(バックフィルジョブがサーキットブレーカーで停止するまで
    # 連鎖的に失敗)。
    shares = pd.Series({pd.Timestamp("2023-08-22"): 41_300_000})
    splits = pd.Series({pd.Timestamp("2023-11-15"): 1.466666667})

    adjusted = _apply_split_adjustment(shares, splits)

    assert adjusted[pd.Timestamp("2023-08-22")] == pytest.approx(41_300_000 * 1.466666667)


def test_split_adjustment_multiple_splits_compound():
    # 2つの分割(2:1、その後3:1)を両方またぐ最古の観測値は6倍される
    shares = pd.Series(
        {
            pd.Timestamp("2020-01-01"): 10_000_000,
            pd.Timestamp("2021-06-01"): 20_000_000,  # 1回目の分割後
            pd.Timestamp("2022-06-01"): 60_000_000,  # 2回目の分割後
        }
    )
    splits = pd.Series({pd.Timestamp("2021-01-01"): 2.0, pd.Timestamp("2022-01-01"): 3.0})

    adjusted = _apply_split_adjustment(shares, splits)

    assert adjusted[pd.Timestamp("2020-01-01")] == 10_000_000 * 2 * 3
    assert adjusted[pd.Timestamp("2021-06-01")] == 20_000_000 * 3
    assert adjusted[pd.Timestamp("2022-06-01")] == 60_000_000
