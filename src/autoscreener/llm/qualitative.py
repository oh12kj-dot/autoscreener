"""定性評価の構造化出力スキーマとリクエスト組み立て(K-9)。純関数のみ。

**なぜ点数(0〜100の float)を出させないか。** 点数にした瞬間、それは
`scores` の因子と同じ見た目になり、いつか誰かが加重平均に入れる。入った時点で
定量モデルの再現性——同じ日・同じ設定で再計算したら同じ順位が出るという
性質——が失われ、バックテスト(14.3)の意味が無くなる。順序尺度(low /
medium / high)なら、足し算するには明示的な数値化が要るので、混入が事故では
なく意図的な決定になる。

**`conviction` が測っているものにも注意が要る。** これは「投資として有望か」
ではなく「開示が具体的で事業構造が追えるか」である。開示が丁寧な会社が良い
投資先とは限らない——両者を混同すると、IR が上手い会社を上位に並べる装置に
なる。フィールド名ではなく `QUALITATIVE_RUBRIC` の定義がこの区別を担っている。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoscreener.llm.prompts import QUALITATIVE_RUBRIC, cached_system


class QualitativeAssessment(BaseModel):
    """構造化出力のスキーマ。`messages.parse` の `output_format` に渡す。

    Pydantic モデルにしているのは、SDK が JSON Schema への変換と検証を
    やってくれるため——自前で `json.loads` して `KeyError` を防ぐ分岐を書くより、
    契約違反を1か所(`LlmParseFailure`)に集約できる。
    """

    business_summary: str = Field(description="2文以内。何を売って稼いでいるか。")
    moat_evidence: list[str] = Field(
        description="参入障壁・乗り換えコスト・規模の経済について原文が述べている根拠。無ければ空配列。"
    )
    key_risks: list[str] = Field(
        description="事業の継続性・成長率に効くリスク。どの10-Kにもある定型文は除く。"
    )
    evidence_gaps: list[str] = Field(
        description="この抜粋だけでは判断できず、人間が別途調べる必要がある点。"
    )
    conviction: Literal["low", "medium", "high"] = Field(
        description="開示の具体性と事業構造の追いやすさ。投資妙味の評価ではない。"
    )
    conviction_rationale: str = Field(description="その水準にした理由。1〜2文。")


@dataclass(frozen=True)
class QualitativeInput:
    """1銘柄ぶんの入力。複数セクションを連結して渡す。"""

    symbol: str
    as_of: datetime.date
    # (見出し, 本文) の並び。見出しは `SECTION_LABELS` の値を想定。
    excerpts: list[tuple[str, str]]


def qualitative_system() -> list[dict[str, Any]]:
    return cached_system(QUALITATIVE_RUBRIC)


def build_qualitative_user_message(payload: QualitativeInput) -> str:
    """可変部分だけを組み立てる(指示はシステム側=キャッシュ側にある)。"""
    parts = [f"銘柄: {payload.symbol}", f"基準日: {payload.as_of.isoformat()}"]
    for label, text in payload.excerpts:
        parts.append(f"\n---- {label} ここから ----\n{text}\n---- {label} ここまで ----")
    return "\n".join(parts)


def assessment_to_dict(assessment: QualitativeAssessment) -> dict[str, Any]:
    """`llm_analyses.data` に入れる形。

    `advisory` を**データ側にも**立てておく。表(`llm_analyses`)に隔離してある
    という構造上の保証に加えて、JSONを単体で取り出して眺めた人にも、これが
    ゲートやスコアに使ってはいけない値だと分かるようにするため。
    """
    return {
        "advisory": True,
        "not_used_in_gates_or_scores": True,
        **assessment.model_dump(),
    }


def qualitative_json_schema() -> dict[str, Any]:
    """Batch API 用の生スキーマ。

    `messages.parse` は Pydantic モデルを受け取ってくれるが、Batch API の
    リクエストには `output_config.format` に**生のJSON Schema**を入れる必要が
    ある(バッチの中身は素の Messages API リクエストであり、SDKのパース
    ヘルパは通らない)。`additionalProperties: false` を明示するのは、
    厳密モードの要件であると同時に、**モデルが余計なキーを足して返しても
    黙って通らないようにするため**——余計なキーは大抵、指示に無い自作の
    「総合点」である。
    """
    schema = QualitativeAssessment.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def parse_qualitative_json(text: str) -> QualitativeAssessment:
    """Batch API から返ったテキストを `QualitativeAssessment` に検証する。

    単発呼び出し(`LlmClient.parse_structured`)ではSDKがやってくれる工程を、
    バッチ経路のためにここで補う。失敗は `LlmParseFailure` に揃える——
    呼び出し側が経路の違いで分岐しなくて済むようにするため。
    """
    import json as _json

    from pydantic import ValidationError as _ValidationError

    from autoscreener.llm.errors import LlmParseFailure

    try:
        raw = _json.loads(text)
    except ValueError as exc:
        raise LlmParseFailure(f"構造化出力がJSONとして読めない: {exc}") from exc
    try:
        return QualitativeAssessment.model_validate(raw)
    except _ValidationError as exc:
        raise LlmParseFailure(f"構造化出力がスキーマに合わない: {exc}") from exc
