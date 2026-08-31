"""上場廃止銘柄の決済(27.11)。

27.11は「上場廃止銘柄を単に捨てると、負けの極端値だけが標本から消えてKPIが
実態より良く出る」ため、最終観測価格で決済することを定めている。ところが
`_realized_return` は清算価格に**評価日時点の終値**(=建玉した瞬間の株価)を
使っていたため、実現リターンがほぼ0%になっていた。負けの極端値が消える代わりに
中立値へ置き換わるだけで、生存バイアスは形を変えて残っていた。
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from autoscreener.backtest.runner import RebalanceSlice, _realized_return

AS_OF = datetime.date(2025, 1, 6)
TARGET = AS_OF + datetime.timedelta(days=365)


def _slice(**overrides) -> RebalanceSlice:
    base = {
        "as_of": AS_OF,
        "close": {1: 10.0},  # 評価日時点の株価
        "shares": {1: 1_000_000.0},
        "entry_open": {1: 10.0},  # 翌営業日の始値で建てる
        "exit_close": {},
        "final_close": {},
        "median_dollar_volume": {1: 5_000_000.0},
        "last_price_date": {},
    }
    base.update(overrides)
    return RebalanceSlice(**base)


def test_market_settlement_uses_the_exit_price():
    result = _realized_return(1, _slice(exit_close={1: 25.0}), TARGET)
    assert result == (1.5, "market")


def test_delisted_settlement_uses_the_last_observed_close_not_the_entry_day_close():
    # 建玉後に −92% まで下げてから価格が途切れた銘柄。
    price_slice = _slice(
        final_close={1: 0.8},
        last_price_date={1: AS_OF + datetime.timedelta(days=120)},
    )

    realized, settlement = _realized_return(1, price_slice, TARGET)

    assert settlement == "delisted"
    assert realized == 0.8 / 10.0 - 1  # −92%
    # 以前は評価日終値(10.0)で清算していたため ±0% になっていた。
    assert realized < -0.5


def test_delisted_without_any_post_entry_price_is_a_total_loss():
    # B-2(docs/defect_and_edge_audit_2026-08-28.md D-1):価格が全く取れない廃止銘柄は
    # 別区分 `delisted_unpriced` にして、KPIを「含めた/除いた」の両方で出せるようにする。
    price_slice = _slice(last_price_date={1: AS_OF})

    assert _realized_return(1, price_slice, TARGET) == (-1.0, "delisted_unpriced")


def test_still_listed_but_missing_exit_price_is_not_settled():
    # 価格が目標日付近まで観測できている = 生きている。廃止として損失計上しない。
    price_slice = _slice(
        final_close={1: 9.0},
        last_price_date={1: TARGET + datetime.timedelta(days=30)},
    )

    assert _realized_return(1, price_slice, TARGET) is None


def test_no_entry_price_means_no_observation():
    assert _realized_return(1, _slice(entry_open={}), TARGET) is None


def _event(event_type: str, settlement=None, source="sec_edgar"):
    return SimpleNamespace(event_type=event_type, settlement_value_per_share=settlement, source=source)


def test_cash_acquisition_uses_cash_consideration():
    realized, settlement = _realized_return(1, _slice(), TARGET, _event("cash_acquisition", 12.0))
    assert settlement == "cash_acquisition"
    assert realized == pytest.approx(0.2)


def test_bankruptcy_uses_zero_recovery_when_no_settlement_is_disclosed():
    assert _realized_return(1, _slice(), TARGET, _event("bankruptcy")) == (-1.0, "bankruptcy")


def test_stock_acquisition_without_roll_value_is_conservative_unknown():
    assert _realized_return(1, _slice(), TARGET, _event("stock_acquisition")) == (
        -1.0, "unknown_delisting"
    )
