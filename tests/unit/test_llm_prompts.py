"""tests/unit/test_llm_prompts.py(K-9)。DBにもネットワークにも触れない。

ここで固定しているのは、**プロンプトキャッシュが黙って壊れないこと**と、
**指示文を変えたら指紋が変わること**の2点である。どちらも失敗しても例外が
出ないタイプの事故なので、テストで押さえておかないと気づけない
(キャッシュが効かなくなっても、返る答えは正しいまま課金だけが増える)。
"""

from __future__ import annotations

import datetime

from autoscreener.llm.filing_summary import (
    SectionInput,
    build_summary_user_message,
    source_refs,
    summary_system,
)
from autoscreener.llm.prompts import (
    SHARED_GUARDRAILS,
    cached_system,
    prompt_fingerprint,
)
from autoscreener.llm.qualitative import (
    QualitativeInput,
    build_qualitative_user_message,
    qualitative_json_schema,
    qualitative_system,
)
from autoscreener.llm.report import (
    CandidateBrief,
    build_report_user_message,
    report_system,
)

_SECTION = SectionInput(
    symbol="ZZLLM1",
    form="10-K",
    filed_date=datetime.date(2026, 3, 1),
    section="item1a",
    text="Risk Factors body text.",
    accession_number="0001234567-26-000001",
    source_url="https://www.sec.gov/example",
)


def test_cache_control_is_on_the_last_system_block():
    """`cache_control` は最後のブロックに1つだけ付く。

    前方一致なので、印より前は自動的にキャッシュ対象に含まれる。複数付けると
    ブレークポイント(最大4)を無駄に消費する。
    """
    system = summary_system()
    assert len(system) == 2
    assert "cache_control" not in system[0]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}


def test_all_tasks_share_the_same_cached_prefix():
    """3タスクの system の**先頭ブロックが完全に同一**であること。

    プロンプトキャッシュは前方一致なので、ここが1文字でも違うとタスクを跨いだ
    プレフィックス共有が消える。共通の前置きに銘柄名や日付を混ぜてしまう
    改修が入ったら、このテストが落ちる。
    """
    prefixes = {
        summary_system()[0]["text"],
        qualitative_system()[0]["text"],
        report_system()[0]["text"],
    }
    assert prefixes == {SHARED_GUARDRAILS}


def test_system_prompt_contains_no_volatile_content():
    """system 側に、呼び出しごとに変わるもの(銘柄・日付)が混ざっていないこと。

    混ざるとキャッシュは毎回ミスする——エラーは出ず、課金だけが増える。
    """
    for system in (summary_system(), qualitative_system(), report_system()):
        joined = "".join(block["text"] for block in system)
        assert "ZZLLM1" not in joined
        assert str(datetime.date.today().year) not in joined


def test_user_message_carries_the_volatile_parts():
    """可変部分(銘柄・提出日・本文)はユーザーメッセージ側にある。"""
    message = build_summary_user_message(_SECTION)
    assert "ZZLLM1" in message
    assert "2026-03-01" in message
    assert "Risk Factors body text." in message
    assert "0001234567-26-000001" in message
    # 指示文は system 側にしか無い(重複させるとキャッシュの効きが落ちる)。
    assert "絶対規則" not in message


def test_fingerprint_changes_when_the_rubric_changes():
    """ルーブリックを変えたら指紋が変わる(=別物として保存される)。"""
    base = cached_system("rubric A")
    changed = cached_system("rubric B")
    assert prompt_fingerprint(base, "claude-opus-5", "high") != prompt_fingerprint(
        changed, "claude-opus-5", "high"
    )


def test_fingerprint_changes_with_model_and_effort():
    system = summary_system()
    a = prompt_fingerprint(system, "claude-opus-5", "high")
    assert a != prompt_fingerprint(system, "claude-sonnet-5", "high")
    assert a != prompt_fingerprint(system, "claude-opus-5", "low")


def test_fingerprint_ignores_cache_control():
    """`cache_control` は配送方法であって指示の中身ではないので、指紋に効かない。

    ここが効いてしまうと、キャッシュ設定を変えただけで既存の出力が全部
    「別物」になり、作り直し(=課金)が走る。
    """
    with_marker = summary_system()
    without_marker = [{k: v for k, v in b.items() if k != "cache_control"} for b in with_marker]
    assert prompt_fingerprint(with_marker, "claude-opus-5", "high") == prompt_fingerprint(
        without_marker, "claude-opus-5", "high"
    )


def test_fingerprint_is_stable_across_calls():
    """同じ入力なら毎回同じ指紋(dictの順序などに依存しない)。"""
    assert prompt_fingerprint(summary_system(), "claude-opus-5", "high") == prompt_fingerprint(
        summary_system(), "claude-opus-5", "high"
    )


def test_qualitative_schema_is_strict():
    """構造化出力のスキーマが厳密モードの要件を満たすこと。

    `additionalProperties: false` が無いと、モデルが指示に無いキー——たいていは
    自作の「総合点」——を足して返しても黙って通る。
    """
    schema = qualitative_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "business_summary",
        "moat_evidence",
        "key_risks",
        "evidence_gaps",
        "conviction",
        "conviction_rationale",
    }
    # conviction は順序尺度であって数値ではない(点数化して他のスコアに
    # 混ぜられないようにするための設計)。
    assert schema["properties"]["conviction"]["type"] == "string"
    assert schema["properties"]["conviction"]["enum"] == ["low", "medium", "high"]


def test_qualitative_user_message_includes_every_excerpt():
    payload = QualitativeInput(
        symbol="ZZLLM1",
        as_of=datetime.date(2026, 8, 30),
        excerpts=[("Item 1A", "risk body"), ("Item 7", "mdna body")],
    )
    message = build_qualitative_user_message(payload)
    assert "risk body" in message
    assert "mdna body" in message


def test_report_message_is_deterministic_for_the_same_input():
    """同じ候補なら同じ文字列になること(JSONのキー順を固定してある)。

    揺れると、内容が同じでもキャッシュがミスし、指紋の比較もできなくなる。
    """
    candidates = [
        CandidateBrief(rank=1, symbol="AAA", probability=0.01, factors={"b": 2, "a": 1}),
        CandidateBrief(rank=2, symbol="BBB", probability=0.008),
    ]
    day = datetime.date(2026, 8, 30)
    assert build_report_user_message(day, candidates) == build_report_user_message(day, candidates)


def test_report_message_contains_the_freshness_fields():
    """鮮度(`data_age_days` / `price_as_of`)がJSONに載ること。

    A-1と同じ理由——収集が止まっていても当日付の順位は書けてしまうので、
    その事実をレポートに運ぶ必要がある。
    """
    message = build_report_user_message(
        datetime.date(2026, 8, 30),
        [CandidateBrief(rank=1, symbol="AAA", price_as_of="2026-08-20", data_age_days=7)],
    )
    assert "data_age_days" in message
    assert "2026-08-20" in message


def test_source_refs_records_where_to_go_back_to():
    refs = source_refs([_SECTION])
    assert refs == [
        {
            "accession_number": "0001234567-26-000001",
            "form": "10-K",
            "section": "item1a",
            "filed_date": "2026-03-01",
            "source_url": "https://www.sec.gov/example",
        }
    ]
