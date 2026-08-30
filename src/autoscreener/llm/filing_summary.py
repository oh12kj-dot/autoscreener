"""提出書類1セクションの要約リクエストを組み立てる純関数(K-9)。

ネットワークにも DB にも触らない——`collectors/filing_text.py` と同じ方針で、
入力(`SectionInput`)を受け取ってプロンプト文字列を返すだけにする。実際の
呼び出しは `batch/summarize_filings.py` が `LlmClient` を通して行う。
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from autoscreener.llm.prompts import FILING_SUMMARY_RUBRIC, cached_system

# `filing_sections.section` の値と、人間向けの名前。プロンプトに「どの Item を
# 読んでいるか」を明示するために使う——同じ本文でも、リスク要因として読むのと
# MD&A として読むのとでは、拾うべき点が違う。
SECTION_LABELS: dict[str, str] = {
    "item1": "Item 1 — Business(事業の説明)",
    "item1a": "Item 1A — Risk Factors(リスク要因)",
    "item3": "Item 3 — Legal Proceedings(係争)",
    "item7": "Item 7 — MD&A(経営陣による財政状態・経営成績の分析)",
    "ex99": "Exhibit 99 — 決算プレスリリース等の添付",
}


@dataclass(frozen=True)
class SectionInput:
    """要約対象の1セクション。`filing_sections` の1行に対応する。"""

    symbol: str
    form: str
    filed_date: datetime.date
    section: str
    text: str
    accession_number: str
    source_url: str | None = None


def summary_system() -> list[dict[str, Any]]:
    return cached_system(FILING_SUMMARY_RUBRIC)


def build_summary_user_message(section: SectionInput) -> str:
    """可変部分(銘柄・書類・本文)だけを組み立てる。

    **ここに指示文を混ぜない。** 指示はすべてシステム側(キャッシュされる)に
    あり、ユーザーメッセージは毎回変わる部分だけにする。混ぜるとキャッシュの
    前方一致が短くなり、課金が増える。
    """
    label = SECTION_LABELS.get(section.section, section.section)
    header = [
        f"銘柄: {section.symbol}",
        f"書類: {section.form}(accession {section.accession_number})",
        f"提出日: {section.filed_date.isoformat()}",
        f"セクション: {label}",
    ]
    if section.source_url:
        header.append(f"原文URL: {section.source_url}")
    return "\n".join(header) + "\n\n---- 本文ここから ----\n" + section.text + "\n---- 本文ここまで ----"


def source_refs(sections: Sequence[SectionInput]) -> list[dict[str, str]]:
    """`llm_analyses.source_refs` に入れる、根拠の所在。

    出力そのものではなく**何を読んで書いたか**を残す。後から要約の妥当性を
    人間が確かめるとき、accession と URL さえあれば原文に戻れる(本文は
    `Filing` と同じ理由でここには保存しない——数十MBを貯めない)。
    """
    return [
        {
            "accession_number": s.accession_number,
            "form": s.form,
            "section": s.section,
            "filed_date": s.filed_date.isoformat(),
            "source_url": s.source_url or "",
        }
        for s in sections
    ]
