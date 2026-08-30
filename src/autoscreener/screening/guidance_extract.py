"""8-K決算プレスリリースからの会社ガイダンス抽出(K-6)。

**この機能が存在する理由(非対称性)**:決算説明会トランスクリプトは安定した
無料の取得先が無く、有料データベンダー経由でしか取れないことが多い。一方で
ガイダンスの原文——経営陣が自分で置いた売上・利益の数値目標——は、8-K の
EX-99.1(決算プレスリリース)として EDGAR に無料で存在する。この非対称性
(トランスクリプトは有料・ガイダンス原文は無料)こそが、このモジュールを
「決算説明会の要約」ではなく「プレスリリースからの数値抽出」として作る理由。

`filing_sections`(`section='ex99'`)の本文を読むのは呼び出し元
(`batch/collect_guidance.py`)の仕事。ここは純関数のみを置く
(`dilution_outlook.py` / `screening/dilution_capacity.py` と同じ方針)。

原則3:ここで抽出した値は `evaluate_gates` にも `scoring/` にも一切入れない。
表示・チェックリスト・ノート起草だけが読者。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SCALE_MULTIPLIERS: dict[str, float] = {
    "thousand": 1_000.0,
    "million": 1_000_000.0,
    "billion": 1_000_000_000.0,
}

# 対象とする指標。値は本文中の見出し語(大文字小文字を区別しない)。
_METRIC_PATTERNS: dict[str, re.Pattern[str]] = {
    "revenue": re.compile(r"revenue", re.IGNORECASE),
    "adjusted_ebitda": re.compile(r"adjusted\s+EBITDA", re.IGNORECASE),
    "gross_margin": re.compile(r"gross\s+margin", re.IGNORECASE),
}

# 指標語の直後(既定150字)に現れるレンジ表記。
# "$120 million to $125 million" / "$480-$500 million" のどちらも拾う。
_USD_RANGE_PATTERN = re.compile(
    r"\$\s?(?P<low>[0-9][0-9,]*(?:\.[0-9]+)?)\s*(?P<low_scale>million|billion|thousand)?"
    r"\s*(?:to|-|–|—)\s*"
    r"\$?\s?(?P<high>[0-9][0-9,]*(?:\.[0-9]+)?)\s*(?P<high_scale>million|billion|thousand)?",
    re.IGNORECASE,
)
# gross_margin 用。"40% to 42%" のようなパーセントのレンジ。0〜1の比率で返す
# (CustomerConcentration.revenue_pct 等、既存コードの比率カラムと単位を揃える)。
_PCT_RANGE_PATTERN = re.compile(
    r"(?P<low>[0-9]{1,3}(?:\.[0-9]+)?)\s*%?\s*(?:to|-|–|—)\s*(?P<high>[0-9]{1,3}(?:\.[0-9]+)?)\s*%",
    re.IGNORECASE,
)

_RANGE_WINDOW = 150
_PERIOD_LOOKAHEAD = 200
_EVIDENCE_LOOKBACK = 60
_EVIDENCE_LOOKAHEAD = 200

_FY_PATTERN = re.compile(
    r"(?:fiscal\s+(?:year\s+)?|full\s+year\s+)(?P<year>20\d{2})", re.IGNORECASE
)
_FY_SHORT_PATTERN = re.compile(r"\bFY\s?(?P<year>\d{2,4})\b", re.IGNORECASE)
_ORDINAL_QUARTER = {
    "1": "1", "first": "1", "1st": "1",
    "2": "2", "second": "2", "2nd": "2",
    "3": "3", "third": "3", "3rd": "3",
    "4": "4", "fourth": "4", "4th": "4",
}
_Q_WORD_PATTERN = re.compile(
    r"\b(?P<q>1st|2nd|3rd|4th|first|second|third|fourth|[1-4])(?:\s+quarter)\b[^0-9]{0,20}(?P<year>20\d{2})",
    re.IGNORECASE,
)
_Q_SHORT_PATTERN = re.compile(r"\bQ(?P<q>[1-4])\s*(?P<year>20\d{2})\b", re.IGNORECASE)


@dataclass(frozen=True)
class GuidanceItem:
    """会社ガイダンス1件。`low` / `high` は revenue/adjusted_ebitda ならUSD、
    gross_margin なら 0〜1 の比率。`raw_text` は根拠原文(数字だけ出しても
    人間は結局原本を確認しに行くので、必ず残す)。"""

    metric: str  # "revenue" / "adjusted_ebitda" / "gross_margin"
    period_label: str  # "FY2027" / "Q3 2026"
    low: float | None
    high: float | None
    raw_text: str


def _amount_from_range_group(match: re.Match[str], side: str) -> float:
    num = float(match.group(side).replace(",", ""))
    scale = match.group(f"{side}_scale")
    if not scale:
        # "$480-$500 million" のように片方だけ位取り接尾辞が付く自然文の
        # 省略表現に対応する:反対側の接尾辞を借りる。
        other_side = "high" if side == "low" else "low"
        scale = match.group(f"{other_side}_scale")
    if scale:
        num *= _SCALE_MULTIPLIERS[scale.lower()]
    return num


def _period_matches(text: str) -> list[tuple[int, int, str]]:
    """`text` 中のすべての期間表現を (開始位置, 終了位置, 正規化ラベル) で返す。"""
    matches: list[tuple[int, int, str]] = []
    for m in _Q_SHORT_PATTERN.finditer(text):
        matches.append((m.start(), m.end(), f"Q{m.group('q')} {m.group('year')}"))
    for m in _Q_WORD_PATTERN.finditer(text):
        q = _ORDINAL_QUARTER.get(m.group("q").lower())
        if q is not None:
            matches.append((m.start(), m.end(), f"Q{q} {m.group('year')}"))
    for m in _FY_SHORT_PATTERN.finditer(text):
        year = m.group("year")
        if len(year) == 2:
            year = "20" + year
        matches.append((m.start(), m.end(), f"FY{year}"))
    for m in _FY_PATTERN.finditer(text):
        matches.append((m.start(), m.end(), f"FY{m.group('year')}"))
    return matches


def _find_period_label(text: str, metric_start: int, lookahead_end: int) -> str | None:
    """指標語に対応する期間ラベルを決める。

    プレスリリースは「For fiscal 2027, we expect revenue of ... and adjusted
    EBITDA of ...」のように、**冒頭で一度だけ期間を宣言し、複数の指標がそれを
    共有する**書き方が多い。固定長の window では2つ目以降の指標が期間を
    拾えないため、指標語より前にある期間表現のうち**直前(最も近い)もの**を
    文書全体から探す(遡り幅に上限を設けない——ただし「直前」なので、
    別の指標のために書かれた無関係な期間まで遡ることはない)。

    直前に期間が無い場合だけ、"revenue of $X to $Y for fiscal 2027" のように
    指標の直後に期間が来る書き方を探す(既定200字先読み)。
    """
    matches = _period_matches(text)
    before = [m for m in matches if m[1] <= metric_start]
    if before:
        before.sort(key=lambda m: m[1])
        return before[-1][2]

    after = [m for m in matches if metric_start <= m[0] < lookahead_end]
    if after:
        after.sort(key=lambda m: m[0])
        return after[0][2]

    return None


def parse_guidance(text: str) -> list[GuidanceItem]:
    """8-K EX-99.1 本文からガイダンス(指標・期間・レンジ)を抽出する。

    `"we expect revenue of $120 million to $125 million for fiscal 2027"` /
    `"full year 2027 revenue guidance of $480-$500 million"` のように、
    指標語(revenue/adjusted EBITDA/gross margin)の直後にレンジ、その前後に
    期間表現が来る語順を想定する。`"raises"` / `"reaffirms"` のような動詞は
    抽出条件に含めない——それらが付いていても付いていなくても指標語・レンジ・
    期間の並びは変わらないため、動詞の有無で抽出可否が揺れない設計にしている。

    同じ (指標, 期間) の組は1件のみ採用する(同一段落内で表記が重複する場合)。
    """
    items: list[GuidanceItem] = []
    seen: set[tuple[str, str]] = set()

    for metric, metric_pattern in _METRIC_PATTERNS.items():
        for metric_match in metric_pattern.finditer(text):
            window_start = metric_match.end()
            window_end = min(len(text), window_start + _RANGE_WINDOW)
            window = text[window_start:window_end]

            if metric == "gross_margin":
                range_match = _PCT_RANGE_PATTERN.search(window)
                if range_match is None:
                    continue
                low = float(range_match.group("low")) / 100
                high = float(range_match.group("high")) / 100
            else:
                range_match = _USD_RANGE_PATTERN.search(window)
                if range_match is None:
                    continue
                low = _amount_from_range_group(range_match, "low")
                high = _amount_from_range_group(range_match, "high")

            lookahead_end = min(len(text), window_start + _PERIOD_LOOKAHEAD)
            period_label = _find_period_label(text, metric_match.start(), lookahead_end)
            if period_label is None:
                continue

            key = (metric, period_label)
            if key in seen:
                continue
            seen.add(key)

            ev_start = max(0, metric_match.start() - _EVIDENCE_LOOKBACK)
            ev_end = min(len(text), window_start + _EVIDENCE_LOOKAHEAD)
            items.append(
                GuidanceItem(
                    metric=metric,
                    period_label=period_label,
                    low=low,
                    high=high,
                    raw_text=text[ev_start:ev_end].strip(),
                )
            )

    return items
