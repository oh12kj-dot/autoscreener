"""tests/unit/test_filing_text.py(K-2)。

実物の10-K/10-Qに近い断片(目次と本文の両方にItem見出しがある例を必ず含む)
を文字列定数として置く。ネットワークには一切出ない。
"""

from __future__ import annotations

from autoscreener.collectors.filing_text import split_sections

# 目次(TOC)と本文の両方に "Item 1A." 等が出現する、実物に近い10-K断片。
# TOCは見出しが数行おきに密集しており、本文は各セクションが長い段落を持つ
# ——`split_sections` が「最も長い区間を生む出現」を採ることを検証する主眼。
_10K_TEXT = """
UNITED STATES SECURITIES AND EXCHANGE COMMISSION
FORM 10-K

TABLE OF CONTENTS

PART I
Item 1. Business 3
Item 1A. Risk Factors 8
Item 1B. Unresolved Staff Comments 20
Item 3. Legal Proceedings 21
PART II
Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations 25
Item 7A. Quantitative and Qualitative Disclosures About Market Risk 40

PART I

Item 1. Business

We design, manufacture, and sell widgets for the industrial sector. """ + (
    "Our widgets are used across a broad range of end markets including manufacturing, "
    "logistics, and energy infrastructure. " * 40
) + """

Item 1A. Risk Factors

Investing in our common stock involves a high degree of risk. """ + (
    "You should carefully consider the risks described below, together with the other "
    "information in this Annual Report on Form 10-K, before making an investment decision. " * 40
) + """

Item 1B. Unresolved Staff Comments

None.

Item 3. Legal Proceedings

We are not currently a party to any material legal proceedings. From time to time, we may be
involved in litigation arising in the ordinary course of our business.

PART II

Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations

The following discussion should be read in conjunction with our consolidated financial
statements. """ + (
    "Revenue increased year over year, driven primarily by growth in our core product lines. " * 40
) + """
As described in Item 1A. Risk Factors above, our business faces a number of risks that could
affect these results.

Item 7A. Quantitative and Qualitative Disclosures About Market Risk

We are exposed to market risk from changes in interest rates and foreign currency exchange rates.
"""


def test_split_sections_10k_extracts_all_four_items():
    sections = split_sections(_10K_TEXT, "10-K")
    assert set(sections) == {"item1", "item1a", "item3", "item7"}


def test_split_sections_10k_picks_body_not_toc():
    sections = split_sections(_10K_TEXT, "10-K")
    # TOCの短い行ではなく、本文の長い段落が採用されていること。
    assert "widgets for the industrial sector" in sections["item1"]
    assert len(sections["item1"]) > 500
    assert "high degree of risk" in sections["item1a"]
    assert len(sections["item1a"]) > 500


def test_split_sections_10k_item3_is_short_but_correct():
    sections = split_sections(_10K_TEXT, "10-K")
    assert "not currently a party to any material legal proceedings" in sections["item3"]
    # Item 1A. Risk Factors の本文が紛れ込んでいないこと。
    assert "high degree of risk" not in sections["item3"]


def test_split_sections_10k_item7_stops_before_item7a():
    sections = split_sections(_10K_TEXT, "10-K")
    assert "Revenue increased year over year" in sections["item7"]
    assert "market risk from changes in interest rates" not in sections["item7"]


def test_split_sections_10k_item7_contains_cross_reference_without_bleeding():
    # 本文中の "Item 1A. Risk Factors above" という相互参照が item1a の内容を
    # 上書き/汚染しないこと(item1a自体は最初の長い出現の本文が採用されるべき)。
    # 見出し正規表現は "Item 1A" までしか消費しないため、続く見出しタイトル
    # ("Risk Factors")は本文側の先頭に残る——それ自体は許容する。
    sections = split_sections(_10K_TEXT, "10-K")
    assert "Investing in our common stock" in sections["item1a"]
    assert sections["item1a"].index("Investing in our common stock") < 50


def test_split_sections_missing_item_not_included_as_empty_string():
    text_without_item3 = (
        "TABLE OF CONTENTS\nItem 1. Business 3\nItem 1A. Risk Factors 8\n\n"
        "Item 1. Business\n\n"
        + ("We design, manufacture, and sell widgets for the industrial sector. " * 30)
        + "\n\nItem 1A. Risk Factors\n\n"
        + ("Investing in our common stock involves a high degree of risk. " * 30)
    )
    sections = split_sections(text_without_item3, "10-K")
    assert "item3" not in sections
    assert "item1" in sections


def test_split_sections_unknown_form_returns_empty_dict():
    assert split_sections(_10K_TEXT, "8-K") == {}


def test_split_sections_empty_text_returns_empty_dict():
    assert split_sections("", "10-K") == {}


# --- 表記ゆれ耐性 -------------------------------------------------------------


def test_split_sections_tolerates_case_and_nbsp_and_no_space():
    text = (
        "TABLE OF CONTENTS\nITEM 1A. Risk Factors 8\n\n"
        "ITEM&nbsp;1A. Risk Factors\n\n"
        + "Our risk factors are significant and numerous in this discussion. " * 30
        + "\n\nItem1B. Unresolved Staff Comments\n\nNone.\n"
    )
    sections = split_sections(text, "10-K")
    assert "item1a" in sections
    assert "significant and numerous" in sections["item1a"]


def test_split_sections_tolerates_newlines_inside_heading_area():
    text = (
        "TABLE OF CONTENTS\nItem 7.\nManagement's Discussion 25\n\n"
        "Item 7.\nManagement's Discussion and Analysis\n\n"
        + "Our results of operations for the year are discussed in detail below. " * 30
        + "\n\nItem 7A.\nQuantitative Disclosures\n\nShort risk discussion.\n"
    )
    sections = split_sections(text, "10-K")
    assert "item7" in sections
    assert "results of operations" in sections["item7"]


# --- 10-Q:item1a/item2(→item7に正規化) だけを対象にする ----------------------

_10Q_TEXT = """
FORM 10-Q

TABLE OF CONTENTS

PART I
Item 1. Financial Statements 3
Item 2. Management's Discussion and Analysis 15
PART II
Item 1. Legal Proceedings 30
Item 1A. Risk Factors 31

PART I

Item 1. Financial Statements

""" + ("Condensed consolidated balance sheet data appears below in tabular form. " * 20) + """

Item 2. Management's Discussion and Analysis

""" + (
    "Revenue for the quarter grew due to continued demand for our products in existing markets. " * 40
) + """

PART II

Item 1. Legal Proceedings

We are not currently a party to any material legal proceedings.

Item 1A. Risk Factors

""" + ("There have been no material changes to the risk factors previously disclosed. " * 30) + """
"""


def test_split_sections_10q_returns_only_item1a_and_item7():
    sections = split_sections(_10Q_TEXT, "10-Q")
    assert set(sections) == {"item1a", "item7"}


def test_split_sections_10q_item7_is_mdna_not_financial_statements():
    sections = split_sections(_10Q_TEXT, "10-Q")
    assert "Revenue for the quarter grew" in sections["item7"]
    assert "Condensed consolidated balance sheet" not in sections["item7"]


def test_split_sections_10q_item1a_is_part_ii_risk_update():
    sections = split_sections(_10Q_TEXT, "10-Q")
    assert "no material changes to the risk factors" in sections["item1a"]
