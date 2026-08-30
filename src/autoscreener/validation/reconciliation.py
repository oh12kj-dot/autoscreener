"""yfinance値とSEC XBRL値の突合(30.5.3)。

元文書 第00節。**桁違いの誤りを潰すのが目的**であり、小数点以下の一致を
求めるものではない。yfinanceの値はTTM・調整後・通貨換算後であることがあり、
数%の差は正常。閾値を緩めに取り、**それでも合わないものだけを拾う**。

**設計上の注記**:元計画は `scores.inputs`(`MoicInputs`)を突合の元にする
想定だったが、`MoicInputs` は `net_debt`(有利子負債−現金の合成値)しか
保持せず、株式数・現金・負債を個別のyfinance生値としては保存していない。
そのため実装では `raw_snapshots.payload.info` / `balance_sheet` の該当項目
(モデルが実際に読んでいる生値そのもの)を `model_inputs` として渡す設計に
した——「モデルが使っている値」を検算するという目的自体は変えていない。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

# 相対差がこれを超えたら不一致とみなす。25%は「四半期1つ分の差」に相当し、
# TTMの期ずれで説明できる範囲の外側。
DEFAULT_TOLERANCE = 0.25
# 桁違い(10倍以上)は別扱い。単位の罠(13.5の debtToEquity 型の欠陥)の疑い。
MAGNITUDE_THRESHOLD = 5.0

MATCH = "match"
MISMATCH = "mismatch"
MAGNITUDE_MISMATCH = "magnitude_mismatch"
UNAVAILABLE = "unavailable"

# 概念名 → model_inputs のキー名。
CONCEPTS: tuple[str, ...] = ("revenue", "shares_outstanding", "cash", "liabilities")

# **期間を持つ概念(フロー)と、時点の値(ストック)を分ける。**
# XBRL の companyfacts には、同じ 10-K の同じ提出日で 3か月・6か月・9か月・
# 12か月の売上が**すべて**入っている。`filed_date` の新しさだけで1件を選ぶと、
# モデル側(yfinance の年次売上)に対して四半期の値をぶつけることになり、
# 健全な会社が軒並み「不一致」になる。実測(DAN、2026-08-30)では
# モデル 7,662百万 対 SEC 3,716百万 = 106% 差 という所見が出たが、これは
# データの誤りではなく**期間の取り違え**だった。誤検出は見逃しより有害である
# ——人間が突合結果を信用しなくなり、突合そのものが機能しなくなるため。
FLOW_CONCEPTS: frozenset[str] = frozenset({"revenue"})

# 年次とみなす期間の長さ(日)。決算期は52/53週制の企業があるので幅を持たせる。
ANNUAL_MIN_DAYS = 300
ANNUAL_MAX_DAYS = 400


@dataclass(frozen=True)
class XbrlFactView:
    concept: str  # "revenue" / "shares_outstanding" / "cash" / "liabilities"
    tag: str
    value: float
    period_end: datetime.date
    filed_date: datetime.date
    # 期間の長さ(`period_end - period_start`)を判定するための開始日。
    # **既定 None は「期間が不明」を意味し、従来どおりの挙動になる**——
    # 既存の呼び出し側(API・CLI・ノート起草)を一斉に直さずに済ませるため。
    period_start: datetime.date | None = None


@dataclass(frozen=True)
class ReconciliationItem:
    concept: str  # "revenue" / "shares_outstanding" / "cash" / "liabilities"
    model_value: float | None  # モデルが使っている値(yfinance由来)
    sec_value: float | None  # XBRL値
    sec_tag: str | None
    sec_period_end: datetime.date | None
    sec_filed_date: datetime.date | None
    relative_diff: float | None
    status: str  # "match" / "mismatch" / "magnitude_mismatch" / "unavailable"


def _is_annual(fact: XbrlFactView) -> bool:
    """年次(12か月)の期間を持つ fact か。期間が不明なら False。"""
    if fact.period_start is None:
        return False
    days = (fact.period_end - fact.period_start).days
    return ANNUAL_MIN_DAYS <= days <= ANNUAL_MAX_DAYS


def _sort_key(fact: XbrlFactView) -> tuple[datetime.date, datetime.date]:
    """新しさの順序。`filed_date` が同じ(=同一提出)なら `period_end` で決める。

    `filed_date` だけで比較すると、同じ 10-K に含まれる複数期間の値が
    **辞書順という実装の偶然**で選ばれてしまう。決定的な順序を明示する。
    """
    return (fact.filed_date, fact.period_end)


def _latest_fact_per_concept(
    facts: list[XbrlFactView],
    *,
    as_of: datetime.date,
    model_period_ends: dict[str, datetime.date] | None = None,
) -> dict[str, XbrlFactView]:
    """概念ごとに、`filed_date <= as_of` を満たす最新の1件を選ぶ。

    同じ期に複数の値がある場合(修正再提出・後続の10-Kでの再掲)は、
    `filed_date` の最も新しいものを採る(30.5.2)——値が変わった事実自体が
    リステートメントの裏付けになるため、上書きはXbrlFact保存時ではなく
    ここ(読み出し側)で行う。

    **フロー概念(売上)は年次の fact だけを候補にする。** モデル側の値が
    yfinance の年次売上だからで、四半期の値と比べても意味がない。年次の
    fact が1件も無い場合(期間情報を持たない古い保存分など)に限り、
    従来どおり全候補から選ぶ——突合が「できない」より「粗くてもできる」
    ほうがよいが、その場合は期間の取り違えが残りうることを承知で使う。

    **`model_period_ends` は「モデル側の値がいつ時点のものか」を概念ごとに
    与える。** これが要るのは `liabilities` である:モデルは yfinance の
    **年次**貸借対照表(例 2025-12-31)を読むのに対し、XBRL の最新は直近の
    10-Q(例 2026-06-30)になる。事業売却などで残高が動いた会社では、
    どちらも正しい値なのに 67% の「不一致」が出る(DAN で実測)。時点を
    合わせずに比べれば、突合は誤検出だけを生む装置になる。
    与えられた概念は**その日付に最も近い period_end** を採り、与えられない
    概念(株式数・現金——モデル側が「現在値」で日付を持たない)は従来どおり
    最新を採る。
    """
    candidates: dict[str, list[XbrlFactView]] = {}
    for fact in facts:
        if fact.filed_date > as_of:
            continue
        candidates.setdefault(fact.concept, []).append(fact)

    targets = model_period_ends or {}
    latest: dict[str, XbrlFactView] = {}
    for concept, concept_facts in candidates.items():
        pool = concept_facts
        if concept in FLOW_CONCEPTS:
            annual = [f for f in concept_facts if _is_annual(f)]
            if annual:
                pool = annual
        target = targets.get(concept)
        if target is not None:
            # 時点を合わせる。同じ距離なら提出が新しいほう(修正再提出を採る)。
            latest[concept] = min(
                pool, key=lambda f: (abs((f.period_end - target).days), -f.filed_date.toordinal())
            )
        else:
            latest[concept] = max(pool, key=_sort_key)
    return latest


def _classify(model_value: float | None, sec_value: float | None, tolerance: float) -> tuple[float | None, str]:
    if model_value is None or sec_value is None:
        return None, UNAVAILABLE
    if sec_value == 0:
        # ゼロ除算を避ける。SEC値が厳密に0の概念(無借金企業のliabilities等)は
        # 実在するため、model_valueも0に近ければmatch、そうでなければmismatch。
        relative_diff = None if model_value == 0 else float("inf")
        status = MATCH if model_value == 0 else MISMATCH
        return relative_diff, status

    relative_diff = abs(model_value - sec_value) / abs(sec_value)
    ratio = abs(model_value) / abs(sec_value) if sec_value != 0 else float("inf")
    if ratio >= MAGNITUDE_THRESHOLD or ratio <= 1.0 / MAGNITUDE_THRESHOLD:
        return relative_diff, MAGNITUDE_MISMATCH
    if relative_diff > tolerance:
        return relative_diff, MISMATCH
    return relative_diff, MATCH


def reconcile(
    model_inputs: dict[str, float | None],
    facts: list[XbrlFactView],
    *,
    as_of: datetime.date,
    tolerance: float = DEFAULT_TOLERANCE,
    model_period_ends: dict[str, datetime.date] | None = None,
) -> list[ReconciliationItem]:
    """4概念それぞれについて突合する。片方が無ければ status="unavailable"。

    `model_period_ends` にモデル側の基準日(概念ごと)を渡すと、SEC側は
    その日に最も近い期を選ぶ。渡さなければ従来どおり最新を選ぶ。
    """
    latest_facts = _latest_fact_per_concept(facts, as_of=as_of, model_period_ends=model_period_ends)
    items: list[ReconciliationItem] = []
    for concept in CONCEPTS:
        model_value = model_inputs.get(concept)
        fact = latest_facts.get(concept)
        sec_value = fact.value if fact is not None else None
        relative_diff, status = _classify(model_value, sec_value, tolerance)
        items.append(
            ReconciliationItem(
                concept=concept,
                model_value=model_value,
                sec_value=sec_value,
                sec_tag=fact.tag if fact is not None else None,
                sec_period_end=fact.period_end if fact is not None else None,
                sec_filed_date=fact.filed_date if fact is not None else None,
                relative_diff=relative_diff,
                status=status,
            )
        )
    return items
