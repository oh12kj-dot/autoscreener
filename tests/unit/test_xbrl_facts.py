"""tests/unit/test_xbrl_facts.py(30.5.6)。"""

from __future__ import annotations

from autoscreener.validation.xbrl_facts import extract_all_concepts, extract_concept_facts

_COMPANY_FACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "end": "2026-06-30",
                            "val": 1000000,
                            "form": "10-Q",
                            "accn": "0001234567-26-000001",
                            "filed": "2026-07-15",
                            "fy": 2026,
                            "fp": "Q2",
                        }
                    ]
                }
            },
            "CashAndCashEquivalentsAtCarryingValue": {
                "units": {
                    "USD": [
                        {
                            "end": "2026-06-30",
                            "val": 500000,
                            "form": "10-Q",
                            "accn": "0001234567-26-000001",
                            "filed": "2026-07-15",
                        }
                    ]
                }
            },
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "end": "2026-07-01",
                            "val": 10000000,
                            "form": "10-Q",
                            "accn": "0001234567-26-000001",
                            "filed": "2026-07-15",
                        }
                    ]
                }
            }
        },
    }
}


def test_fallback_tag_priority_uses_first_available():
    facts = extract_concept_facts(_COMPANY_FACTS, "revenue")
    assert len(facts) == 1
    assert facts[0].tag == "Revenues"
    assert facts[0].value == 1000000


def test_missing_primary_tag_falls_back_none_when_no_data():
    # "liabilities" タグが無いケース
    facts = extract_concept_facts(_COMPANY_FACTS, "liabilities")
    assert facts == []


def test_shares_outstanding_from_dei_taxonomy():
    facts = extract_concept_facts(_COMPANY_FACTS, "shares_outstanding")
    assert len(facts) == 1
    assert facts[0].taxonomy == "dei"
    assert facts[0].value == 10000000


def test_extract_all_concepts_covers_the_full_concept_map():
    # B-3(docs/defect_and_edge_audit_2026-08-28.md I-1):概念セットを大幅拡張した。
    from autoscreener.validation.xbrl_facts import CONCEPT_TAGS

    result = extract_all_concepts(_COMPANY_FACTS)
    assert set(result.keys()) == set(CONCEPT_TAGS)
    # 元からある突合4概念は引き続き取れること。
    assert {"revenue", "shares_outstanding", "cash", "liabilities"} <= set(result)


def test_malformed_entry_is_skipped_without_raising():
    broken = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"end": "not-a-date", "val": "x", "filed": "2026-07-15", "form": "10-Q", "accn": "a"},
                            {
                                "end": "2026-06-30",
                                "val": 1000,
                                "filed": "2026-07-15",
                                "form": "10-Q",
                                "accn": "b",
                            },
                        ]
                    }
                }
            }
        }
    }
    facts = extract_concept_facts(broken, "revenue")
    assert len(facts) == 1
    assert facts[0].value == 1000
