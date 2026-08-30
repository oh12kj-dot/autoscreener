"""tests/unit/test_customer_concentration.py(K-3)。ネットワークには出ない。"""

from __future__ import annotations

import datetime

from autoscreener.screening.customer_concentration import (
    concentration_drop,
    extract_from_xbrl,
    parse_concentration_text,
)

# --- parse_concentration_text -------------------------------------------------


def test_parse_one_customer_percentage():
    text = "During fiscal 2025, one customer accounted for 23% of revenue."
    mentions = parse_concentration_text(text)
    assert len(mentions) == 1
    assert mentions[0].customer_label == "customer_1"
    assert mentions[0].revenue_pct == 0.23


def test_parse_named_customer_with_decimal_percentage():
    text = "Customer A represented approximately 15.4% of our total revenues in fiscal 2025."
    mentions = parse_concentration_text(text)
    assert len(mentions) == 1
    assert mentions[0].customer_label == "Customer A"
    assert mentions[0].revenue_pct == 0.154


def test_parse_two_customers_two_percentages():
    text = "During the year, two customers accounted for 31% and 12% of total revenue, respectively."
    mentions = parse_concentration_text(text)
    assert len(mentions) == 2
    labels = {m.customer_label for m in mentions}
    assert labels == {"customer_1", "customer_2"}
    pcts = sorted(m.revenue_pct for m in mentions)
    assert pcts == [0.12, 0.31]


def test_parse_largest_customer_phrasing():
    text = "Our largest customer accounted for 18% of net sales during fiscal 2025."
    mentions = parse_concentration_text(text)
    assert len(mentions) == 1
    assert mentions[0].revenue_pct == 0.18


def test_parse_named_customers_paired_in_order():
    text = "Customer A represented 15% of revenue and Customer B represented 11% of revenue."
    mentions = parse_concentration_text(text)
    assert len(mentions) == 2
    by_label = {m.customer_label: m.revenue_pct for m in mentions}
    assert by_label == {"Customer A": 0.15, "Customer B": 0.11}


def test_parse_concentration_of_credit_risk_boilerplate():
    text = (
        "Concentration of credit risk: financial instruments that potentially subject the "
        "Company to concentrations of credit risk consist of accounts receivable. One customer "
        "accounted for 27% of accounts receivable at year end."
    )
    mentions = parse_concentration_text(text)
    assert any(m.revenue_pct == 0.27 for m in mentions)


def test_parse_requires_customer_word_in_same_sentence():
    # "%"はあるが同じ文に customer が無い(前の文にしか無い)ケースは拾わない。
    text = "Our top customer relationship remains strong. Total revenue grew 23% year over year."
    mentions = parse_concentration_text(text)
    assert mentions == []


def test_parse_requires_percentage_present():
    text = "Our customer base is highly diversified across several industries."
    mentions = parse_concentration_text(text)
    assert mentions == []


def test_parse_ignores_out_of_range_percentages():
    text = "Our customer contracts renew annually and pricing increased 150% over five years."
    mentions = parse_concentration_text(text)
    assert mentions == []


def test_parse_empty_text_returns_empty_list():
    assert parse_concentration_text("") == []


# --- extract_from_xbrl ---------------------------------------------------------


def test_extract_from_xbrl_returns_empty_without_axis_info():
    # companyconcept APIの実際のレスポンス形(軸情報が無い)を模す。
    payload = {
        "units": {
            "pure": [
                {"end": "2025-12-31", "val": 0.23, "accn": "0001234567-26-000001", "fy": 2025, "fp": "FY"},
            ]
        }
    }
    assert extract_from_xbrl(payload) == []


def test_extract_from_xbrl_uses_axis_info_when_present():
    payload = {
        "units": {
            "pure": [
                {
                    "end": "2025-12-31",
                    "val": 0.23,
                    "accn": "0001234567-26-000001",
                    "fy": 2025,
                    "segment": {"member": "CustomerAMember"},
                },
            ]
        }
    }
    facts = extract_from_xbrl(payload)
    assert len(facts) == 1
    assert facts[0].customer_label == "CustomerAMember"
    assert facts[0].revenue_pct == 0.23
    assert facts[0].period_end == datetime.date(2025, 12, 31)


def test_extract_from_xbrl_handles_missing_units():
    assert extract_from_xbrl({}) == []


def test_extract_from_xbrl_ignores_out_of_range_values():
    payload = {
        "units": {
            "pure": [
                {"end": "2025-12-31", "val": 1.5, "segment": {"member": "CustomerAMember"}},
            ]
        }
    }
    assert extract_from_xbrl(payload) == []


# --- concentration_drop ---------------------------------------------------------


def test_concentration_drop_disclosure_disappeared():
    history = [
        (datetime.date(2024, 12, 31), 0.23),
        (datetime.date(2025, 12, 31), None),
    ]
    result = concentration_drop(history)
    assert result.triggered is True
    assert result.reason == "disclosure_disappeared"


def test_concentration_drop_pct_dropped_beyond_threshold():
    history = [
        (datetime.date(2024, 12, 31), 0.30),
        (datetime.date(2025, 12, 31), 0.20),
    ]
    result = concentration_drop(history, drop_threshold_points=0.05)
    assert result.triggered is True
    assert result.reason == "pct_dropped"


def test_concentration_drop_small_change_does_not_trigger():
    history = [
        (datetime.date(2024, 12, 31), 0.30),
        (datetime.date(2025, 12, 31), 0.28),
    ]
    result = concentration_drop(history, drop_threshold_points=0.05)
    assert result.triggered is False
    assert result.reason is None


def test_concentration_drop_stable_disclosure_does_not_trigger():
    history = [
        (datetime.date(2024, 12, 31), 0.23),
        (datetime.date(2025, 12, 31), 0.24),
    ]
    result = concentration_drop(history)
    assert result.triggered is False


def test_concentration_drop_insufficient_history_does_not_trigger():
    result = concentration_drop([(datetime.date(2025, 12, 31), 0.23)])
    assert result.triggered is False
    assert result.previous_period is None


def test_concentration_drop_no_prior_disclosure_does_not_trigger_disappearance():
    # 前期からNoneなら「消失」ではない(元々開示が無かっただけ)。
    history = [
        (datetime.date(2024, 12, 31), None),
        (datetime.date(2025, 12, 31), None),
    ]
    result = concentration_drop(history)
    assert result.triggered is False


def test_concentration_drop_sorts_unordered_history():
    history = [
        (datetime.date(2025, 12, 31), 0.10),
        (datetime.date(2024, 12, 31), 0.30),
    ]
    result = concentration_drop(history, drop_threshold_points=0.05)
    assert result.triggered is True
    assert result.previous_period == datetime.date(2024, 12, 31)
    assert result.current_period == datetime.date(2025, 12, 31)
