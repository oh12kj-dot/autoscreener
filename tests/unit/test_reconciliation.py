"""tests/unit/test_reconciliation.py(30.5.6)。"""

from __future__ import annotations

import datetime

from autoscreener.validation.reconciliation import (
    MAGNITUDE_MISMATCH,
    MATCH,
    MISMATCH,
    UNAVAILABLE,
    XbrlFactView,
    reconcile,
)

AS_OF = datetime.date(2026, 8, 28)


def _fact(**kwargs) -> XbrlFactView:
    defaults = dict(
        concept="revenue",
        tag="Revenues",
        value=1000.0,
        period_end=datetime.date(2026, 6, 30),
        filed_date=datetime.date(2026, 7, 15),
    )
    defaults.update(kwargs)
    return XbrlFactView(**defaults)


def test_matching_values_within_tolerance():
    items = reconcile({"revenue": 1050.0}, [_fact(value=1000.0)], as_of=AS_OF)
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.status == MATCH


def test_mismatch_beyond_tolerance():
    items = reconcile({"revenue": 1400.0}, [_fact(value=1000.0)], as_of=AS_OF)  # 40%差
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.status == MISMATCH


def test_magnitude_mismatch_at_1000x():
    items = reconcile({"revenue": 1_000_000.0}, [_fact(value=1000.0)], as_of=AS_OF)
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.status == MAGNITUDE_MISMATCH


def test_missing_model_value_is_unavailable_not_mismatch():
    items = reconcile({}, [_fact(value=1000.0)], as_of=AS_OF)
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.status == UNAVAILABLE


def test_missing_sec_fact_is_unavailable():
    items = reconcile({"revenue": 1000.0}, [], as_of=AS_OF)
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.status == UNAVAILABLE


def test_newest_filed_date_used_when_multiple_facts_for_same_concept():
    old_fact = _fact(value=900.0, filed_date=datetime.date(2026, 5, 1))
    new_fact = _fact(value=1000.0, filed_date=datetime.date(2026, 7, 15))
    items = reconcile({"revenue": 1000.0}, [old_fact, new_fact], as_of=AS_OF)
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.sec_value == 1000.0
    assert revenue_item.status == MATCH


def test_future_filed_date_beyond_as_of_is_excluded():
    future_fact = _fact(value=1000.0, filed_date=datetime.date(2026, 12, 1))
    items = reconcile({"revenue": 1000.0}, [future_fact], as_of=AS_OF)
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.status == UNAVAILABLE


def test_all_four_concepts_present_in_output():
    items = reconcile({}, [], as_of=AS_OF)
    assert {i.concept for i in items} == {"revenue", "shares_outstanding", "cash", "liabilities"}


def test_zero_sec_value_with_zero_model_value_matches():
    items = reconcile({"liabilities": 0.0}, [_fact(concept="liabilities", value=0.0)], as_of=AS_OF)
    liab_item = next(i for i in items if i.concept == "liabilities")
    assert liab_item.status == MATCH


# --- 期間の取り違えの防止(2026-08-30) ---------------------------------------
#
# `xbrl_facts` に実データ(405,373 facts)が入った直後の実測で、健全な会社が
# 「売上 106% 不一致」と判定される所見が出た。原因はデータの誤りではなく、
# 同じ 10-K に入っている四半期の売上を年次の売上とぶつけていたこと。
# 誤検出は見逃しより有害である(人間が突合結果を読まなくなる)。


def _annual(**kwargs) -> XbrlFactView:
    defaults = dict(
        concept="revenue",
        tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        value=7_600_000_000.0,
        period_start=datetime.date(2025, 7, 1),
        period_end=datetime.date(2026, 6, 30),
        filed_date=datetime.date(2026, 7, 15),
    )
    defaults.update(kwargs)
    return XbrlFactView(**defaults)


def _quarterly(**kwargs) -> XbrlFactView:
    defaults = dict(
        concept="revenue",
        tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        value=1_900_000_000.0,
        period_start=datetime.date(2026, 4, 1),
        period_end=datetime.date(2026, 6, 30),
        filed_date=datetime.date(2026, 7, 15),
    )
    defaults.update(kwargs)
    return XbrlFactView(**defaults)


def test_annual_revenue_preferred_over_quarterly_from_same_filing():
    """同一提出に四半期と年次が両方あるとき、年次が選ばれること。"""
    items = reconcile(
        {"revenue": 7_662_000_128.0},
        [_quarterly(), _annual()],
        as_of=AS_OF,
    )
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.sec_value == 7_600_000_000.0
    assert revenue_item.status == MATCH


def test_quarterly_only_still_reconciles_but_flags_mismatch():
    """年次が1件も無ければ従来どおり比較する(突合できないよりはよい)。"""
    items = reconcile({"revenue": 7_662_000_128.0}, [_quarterly()], as_of=AS_OF)
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.sec_value == 1_900_000_000.0
    assert revenue_item.status == MISMATCH


def test_newest_annual_wins_across_fiscal_years():
    older = _annual(
        value=6_000_000_000.0,
        period_start=datetime.date(2024, 7, 1),
        period_end=datetime.date(2025, 6, 30),
        filed_date=datetime.date(2025, 7, 15),
    )
    items = reconcile({"revenue": 7_600_000_000.0}, [older, _annual()], as_of=AS_OF)
    revenue_item = next(i for i in items if i.concept == "revenue")
    assert revenue_item.sec_period_end == datetime.date(2026, 6, 30)


def test_same_filed_date_ties_broken_by_period_end_not_iteration_order():
    """提出日が同じなら period_end の新しいほうを採る(実装の偶然に依存しない)。"""
    a = _annual(period_start=datetime.date(2024, 7, 1), period_end=datetime.date(2025, 6, 30), value=6e9)
    b = _annual(period_start=datetime.date(2025, 7, 1), period_end=datetime.date(2026, 6, 30), value=7.6e9)
    for facts in ([a, b], [b, a]):
        items = reconcile({"revenue": 7.6e9}, facts, as_of=AS_OF)
        revenue_item = next(i for i in items if i.concept == "revenue")
        assert revenue_item.sec_value == 7.6e9


def test_stock_concepts_unaffected_by_period_filter():
    """ストック概念(株式数)は期間を持たないので従来どおり選ばれること。"""
    fact = XbrlFactView(
        concept="shares_outstanding",
        tag="EntityCommonStockSharesOutstanding",
        value=107_576_679.0,
        period_end=datetime.date(2026, 6, 30),
        filed_date=datetime.date(2026, 7, 15),
    )
    items = reconcile({"shares_outstanding": 107_576_679.0}, [fact], as_of=AS_OF)
    item = next(i for i in items if i.concept == "shares_outstanding")
    assert item.status == MATCH


def test_model_period_end_aligns_stock_concept():
    """モデル側が年次(2025-12-31)、SEC側に四半期(2026-06-30)もあるとき、
    基準日を渡せば年次のほうが選ばれること(DAN で実測した誤検出の再発防止)。"""
    annual = XbrlFactView(
        concept="liabilities",
        tag="Liabilities",
        value=6_909_000_000.0,
        period_end=datetime.date(2025, 12, 31),
        filed_date=datetime.date(2026, 2, 20),
    )
    quarterly = XbrlFactView(
        concept="liabilities",
        tag="Liabilities",
        value=4_130_000_000.0,
        period_end=datetime.date(2026, 6, 30),
        filed_date=datetime.date(2026, 7, 30),
    )
    aligned = reconcile(
        {"liabilities": 6_909_000_000.0},
        [annual, quarterly],
        as_of=AS_OF,
        model_period_ends={"liabilities": datetime.date(2025, 12, 31)},
    )
    item = next(i for i in aligned if i.concept == "liabilities")
    assert item.sec_value == 6_909_000_000.0
    assert item.status == MATCH

    # 基準日を渡さなければ従来どおり最新が選ばれる(後方互換)。
    unaligned = reconcile({"liabilities": 6_909_000_000.0}, [annual, quarterly], as_of=AS_OF)
    assert next(i for i in unaligned if i.concept == "liabilities").sec_value == 4_130_000_000.0
