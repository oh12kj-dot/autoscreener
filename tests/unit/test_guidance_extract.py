"""tests/unit/test_guidance_extract.py(K-6)。

`screening/guidance_extract.py` は純関数のみなのでネットワーク・DBに一切
出ない。フィクスチャは実物の決算プレスリリース(8-K EX-99.1)の断片に近い
文面を定数として置く。誤検出しないことのテストを必ず含める。
"""

from __future__ import annotations

from autoscreener.screening.guidance_extract import parse_guidance

# --- 基本形:prompt に挙げられた2つの型 ----------------------------------------


def test_parse_guidance_expect_range_for_fiscal_year():
    text = "We expect revenue of $120 million to $125 million for fiscal 2027."
    items = parse_guidance(text)
    assert len(items) == 1
    item = items[0]
    assert item.metric == "revenue"
    assert item.period_label == "FY2027"
    assert item.low == 120_000_000.0
    assert item.high == 125_000_000.0
    assert "$120 million" in item.raw_text


def test_parse_guidance_full_year_revenue_guidance_shared_scale_suffix():
    text = "Full year 2027 revenue guidance of $480-$500 million."
    items = parse_guidance(text)
    assert len(items) == 1
    item = items[0]
    assert item.metric == "revenue"
    assert item.period_label == "FY2027"
    # "$480-$500 million" は片方にしか付いていない位取り接尾辞を両方へ適用する。
    assert item.low == 480_000_000.0
    assert item.high == 500_000_000.0


# --- reaffirms / raises が付いていても抽出できること ---------------------------


def test_parse_guidance_raises_verb_does_not_block_extraction():
    text = (
        "The Company today raises its full year 2027 revenue guidance to a "
        "range of $480 million to $500 million, up from prior guidance."
    )
    items = parse_guidance(text)
    assert any(i.metric == "revenue" and i.period_label == "FY2027" for i in items)


def test_parse_guidance_reaffirms_verb_does_not_block_extraction():
    text = "The Company reaffirms revenue guidance of $120 million to $125 million for fiscal 2027."
    items = parse_guidance(text)
    assert any(i.metric == "revenue" and i.period_label == "FY2027" for i in items)


# --- adjusted_ebitda / gross_margin -------------------------------------------


def test_parse_guidance_adjusted_ebitda_quarterly():
    text = "We expect adjusted EBITDA of $10 million to $12 million for Q3 2026."
    items = parse_guidance(text)
    assert len(items) == 1
    item = items[0]
    assert item.metric == "adjusted_ebitda"
    assert item.period_label == "Q3 2026"
    assert item.low == 10_000_000.0
    assert item.high == 12_000_000.0


def test_parse_guidance_gross_margin_percentage_range():
    text = "For fiscal 2027, we expect gross margin of 40% to 42%."
    items = parse_guidance(text)
    assert len(items) == 1
    item = items[0]
    assert item.metric == "gross_margin"
    assert item.period_label == "FY2027"
    assert item.low == 0.40
    assert item.high == 0.42


def test_parse_guidance_third_quarter_word_form():
    text = "For the third quarter of 2026, we expect revenue of $50 million to $52 million."
    items = parse_guidance(text)
    assert any(i.period_label == "Q3 2026" for i in items)


# --- 複数指標の同時抽出 ---------------------------------------------------------


def test_parse_guidance_multiple_metrics_in_one_release():
    text = (
        "For fiscal 2027, we expect revenue of $480 million to $500 million and "
        "adjusted EBITDA of $60 million to $65 million."
    )
    items = parse_guidance(text)
    metrics = {i.metric for i in items}
    assert metrics == {"revenue", "adjusted_ebitda"}
    for item in items:
        assert item.period_label == "FY2027"


# --- 誤検出しないことの確認 ------------------------------------------------------


def test_parse_guidance_does_not_treat_reported_actuals_as_range():
    # 実績の言及(revenueが実際に増加した、という報告)であり、ガイダンスではない。
    # "$125 million" と "$104 million" の間に "to" が直接連続しないため
    # レンジとして誤検出しないことを確認する。
    text = (
        "Third quarter revenue increased 20% to $125 million, compared to "
        "$104 million in the prior year period."
    )
    items = parse_guidance(text)
    assert items == []


def test_parse_guidance_metric_without_range_produces_nothing():
    text = "Revenue growth remains a top priority for fiscal 2027."
    items = parse_guidance(text)
    assert items == []


def test_parse_guidance_unrelated_dollar_range_without_metric_keyword():
    text = "Total outstanding debt was $120 million to $125 million as of quarter end."
    items = parse_guidance(text)
    assert items == []
