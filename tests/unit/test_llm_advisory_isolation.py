"""tests/unit/test_llm_advisory_isolation.py(K-9)。

**このファイルが守っているのは設計上の約束そのものである。**
`docs/outside_tenx_implementation_plan_2026-08-28.md` 第618行の原則1——再現性が無く
検証もできない判定をブロッキング条件にしてはならない——を、コメントではなく
テストとして固定する。

LLMの出力は同じ入力でも揺れる。それをゲート(`screening/exclusion_gates.py`)
や定量モデル(`scoring/`)に流し込むと、過去の日付でランキングを再計算しても
当時と同じ結果が出なくなり、バックテスト(14.3)が意味を失う。**この事故は
静かに起きる**——import を1行足すだけで、テストは全部通ったまま再現性だけが
消える。だから import の有無そのものを検査する。

`llm_analyses` 表への参照も同じ理由で禁じる。テーブルを分けたのは、約束を
人間の記憶ではなくスキーマで守るためであり、参照されたらその意味が無い。
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "autoscreener"

# LLMの出力に触れてはならない層。ここが定量モデルの再現性を担っている。
_PROTECTED_PACKAGES = ("screening", "scoring", "backtest", "validation")

_FORBIDDEN_MODULE_PREFIX = "autoscreener.llm"
_FORBIDDEN_NAMES = ("LlmAnalysis", "llm_analyses")


def _protected_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for package in _PROTECTED_PACKAGES:
        files.extend(sorted((_SRC / package).rglob("*.py")))
    return files


def test_protected_packages_exist():
    """対象パッケージが実在すること(パッケージ名を変えたらここで気づく)。

    これが無いと、リネームでグロブが空になり、テストが「何も検査せずに成功」
    という一番まずい状態になる。
    """
    assert _protected_files(), "検査対象のファイルが1件も見つからない"


def test_gates_and_scoring_do_not_import_the_llm_package():
    offenders: list[str] = []
    for path in _protected_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(_FORBIDDEN_MODULE_PREFIX):
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith(_FORBIDDEN_MODULE_PREFIX):
                    offenders.append(f"{path.name}:{node.lineno} from {node.module}")
    assert not offenders, (
        "ゲート/スコアリング層が autoscreener.llm を参照している。"
        "LLMの出力は再現性が無いため、除外や順位づけの根拠にしてはならない: " + "; ".join(offenders)
    )


def test_gates_and_scoring_do_not_read_the_llm_analyses_table():
    offenders: list[str] = []
    for path in _protected_files():
        source = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            for name in _FORBIDDEN_NAMES:
                if name in line:
                    offenders.append(f"{path.name}:{lineno} {name}")
    assert not offenders, (
        "ゲート/スコアリング層が llm_analyses を参照している。"
        "この表は表示・ノート起草・下読み専用である: " + "; ".join(offenders)
    )


def test_llm_package_does_not_write_to_scores_or_gates():
    """逆方向も塞ぐ:LLM層が `scores` を書き換えないこと。

    片方向だけ塞いでも、LLM層が `Score.factors` に書き込めば同じことが起きる。
    """
    offenders: list[str] = []
    llm_files = sorted((_SRC / "llm").rglob("*.py"))
    assert llm_files
    for path in llm_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("autoscreener.scoring") or node.module.startswith(
                    "autoscreener.screening"
                ):
                    offenders.append(f"{path.name}:{node.lineno} from {node.module}")
    assert not offenders, "LLM層が定量モデル側を参照している: " + "; ".join(offenders)


def test_advisory_flag_is_carried_in_the_stored_payload():
    """保存されるJSON自体にも「参考値」の印が付くこと。

    表を分けてあるという構造上の保証に加えて、JSONを単体で取り出して眺めた
    人にも、これがゲートやスコアに使ってはいけない値だと分かるようにする。
    """
    from autoscreener.llm.qualitative import QualitativeAssessment, assessment_to_dict

    payload = assessment_to_dict(
        QualitativeAssessment(
            business_summary="s",
            moat_evidence=[],
            key_risks=[],
            evidence_gaps=[],
            conviction="low",
            conviction_rationale="r",
        )
    )
    assert payload["advisory"] is True
    assert payload["not_used_in_gates_or_scores"] is True
