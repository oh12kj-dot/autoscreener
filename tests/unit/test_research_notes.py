"""tests/unit/test_research_notes.py(30.7.6)。"""

from __future__ import annotations

import pytest

from autoscreener.research.notes import (
    MIN_PREMORTEM_ITEMS,
    MIN_THESIS_BREAK_ITEMS,
    NoteParseError,
    load_all_notes,
    load_note,
)

_COMPLETE = """---
ticker: ABCD
thesis: 3文以内の投資テーゼ
assumptions:
  revenue_growth: {model: 0.4, mine: 0.3}
premortem:
  - cause: a
    indicator: x
    detail: d1
  - cause: b
    indicator: y
    detail: d2
  - cause: c
    indicator: z
    detail: d3
sizing:
  amount_usd: 1000
verification_date: 2026-11-05
exit_plan:
  thesis_break:
    - condition: 粗利率が3四半期連続で低下
      indicator: gross_margin_decline
    - condition: 主要顧客の離脱
      indicator: customer_concentration_disclosed_drop
    - condition: 資金繰りの増資
      indicator: share_count_growth
  trim_rule:
    - at_moic: 3.0
      action: 1/3 を売却
    - at_moic: 6.0
      action: さらに 1/3
  max_hold_review_months: 24
---

本文。
"""

_MISSING_THESIS = _COMPLETE.replace("thesis: 3文以内の投資テーゼ\n", "")


def test_missing_file_returns_none(tmp_path):
    assert load_note("ZZZZ", tmp_path) is None


def test_complete_note_has_no_missing_fields(tmp_path):
    (tmp_path / "ABCD.md").write_text(_COMPLETE, encoding="utf-8")
    note = load_note("ABCD", tmp_path)
    assert note is not None
    assert note.missing_fields == []
    assert note.is_complete is True


def test_missing_thesis_is_detected(tmp_path):
    (tmp_path / "ABCD.md").write_text(_MISSING_THESIS, encoding="utf-8")
    note = load_note("ABCD", tmp_path)
    assert "thesis" in note.missing_fields
    assert note.is_complete is False


def test_fewer_than_three_premortem_items_is_missing(tmp_path):
    text = _COMPLETE.replace(
        """premortem:
  - cause: a
    indicator: x
    detail: d1
  - cause: b
    indicator: y
    detail: d2
  - cause: c
    indicator: z
    detail: d3
""",
        """premortem:
  - cause: a
    indicator: x
    detail: d1
  - cause: b
    indicator: y
    detail: d2
""",
    )
    (tmp_path / "ABCD.md").write_text(text, encoding="utf-8")
    note = load_note("ABCD", tmp_path)
    assert "premortem" in note.missing_fields


def test_missing_exit_plan_is_detected_but_note_still_parses(tmp_path):
    text = _COMPLETE[: _COMPLETE.index("exit_plan:")] + "---\n\n本文。\n"
    (tmp_path / "ABCD.md").write_text(text, encoding="utf-8")
    note = load_note("ABCD", tmp_path)
    assert note is not None  # API は落ちない
    assert "exit_plan.thesis_break" in note.missing_fields
    assert "exit_plan.trim_rule" in note.missing_fields
    assert note.is_complete is False


def test_fewer_than_three_thesis_break_items_is_missing(tmp_path):
    text = _COMPLETE.replace(
        """    - condition: 主要顧客の離脱
      indicator: customer_concentration_disclosed_drop
    - condition: 資金繰りの増資
      indicator: share_count_growth
""",
        "",
    )
    (tmp_path / "ABCD.md").write_text(text, encoding="utf-8")
    note = load_note("ABCD", tmp_path)
    assert MIN_THESIS_BREAK_ITEMS == 3
    assert "exit_plan.thesis_break" in note.missing_fields


def test_malformed_yaml_raises_note_parse_error(tmp_path):
    (tmp_path / "ABCD.md").write_text("---\nticker: ABCD\n  bad indent: [\n---\nbody\n", encoding="utf-8")
    with pytest.raises(NoteParseError):
        load_note("ABCD", tmp_path)


def test_load_all_notes_skips_template(tmp_path):
    (tmp_path / "ABCD.md").write_text(_COMPLETE, encoding="utf-8")
    (tmp_path / "TEMPLATE.md").write_text(_COMPLETE, encoding="utf-8")
    notes = load_all_notes(tmp_path)
    assert set(notes.keys()) == {"ABCD"}


def test_load_all_notes_on_missing_directory_returns_empty(tmp_path):
    notes = load_all_notes(tmp_path / "does_not_exist")
    assert notes == {}


def test_load_all_notes_skips_broken_note_without_raising(tmp_path):
    (tmp_path / "ABCD.md").write_text(_COMPLETE, encoding="utf-8")
    (tmp_path / "BROKEN.md").write_text("---\n[bad yaml\n---\nbody\n", encoding="utf-8")
    notes = load_all_notes(tmp_path)
    assert set(notes.keys()) == {"ABCD"}
