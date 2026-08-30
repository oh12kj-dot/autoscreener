"""tests/unit/test_filing_analysis.py(30.4.5)。"""

from __future__ import annotations

from autoscreener.screening.red_flags import analyze_document_text


def test_going_concern_detected_with_substantial_doubt_cooccurrence():
    text = (
        "In connection with our audit, we noted conditions that raise substantial "
        "doubt about the Company's ability to continue as a going concern."
    )
    result = analyze_document_text(text)
    assert result["going_concern"] is True
    assert "substantial" in result["excerpt"].lower() or "going concern" in result["excerpt"].lower()


def test_going_concern_basis_alone_is_not_flagged():
    text = "The accompanying financial statements have been prepared on a going concern basis."
    result = analyze_document_text(text)
    assert result["going_concern"] is False


def test_material_weakness_negated_is_false():
    text = "Management concluded that no material weaknesses were identified as of the end of the period."
    result = analyze_document_text(text)
    assert result["material_weakness"] is False


def test_material_weakness_positive_is_true():
    text = (
        "We identified a material weakness in our internal control over financial "
        "reporting related to revenue recognition."
    )
    result = analyze_document_text(text)
    assert result["material_weakness"] is True


def test_truncated_flag_is_preserved():
    result = analyze_document_text("no signal here", truncated=True)
    assert result["truncated"] is True


def test_no_match_returns_false_with_empty_excerpt():
    result = analyze_document_text("Ordinary business text with nothing notable.")
    assert result["going_concern"] is False
    assert result["material_weakness"] is False
    assert result["excerpt"] == ""


def test_analyzed_at_defaults_to_today_iso_format():
    import datetime

    result = analyze_document_text("text")
    datetime.date.fromisoformat(result["analyzed_at"])  # raises if malformed
