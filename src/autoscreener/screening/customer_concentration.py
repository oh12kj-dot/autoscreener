"""顧客集中(10%超顧客の開示)の抽出ロジック(K-3、純関数)。

`research/TEMPLATE.md` はプレモーテムの先行指標として
`customer_concentration_disclosed_drop`(主要顧客の10%超開示が消える/最大
顧客比率が大きく落ちる)を要求しているが、これまで人間が10-Kを読んで手入力
していた。ここではその機械版を作る——(1) 本文からの正規表現抽出、(2) XBRL
`ConcentrationRiskPercentage1` からの抽出、(3) 年度をまたいだ「消失/低下」判定
の3つ。すべて純関数(ネットワーク・DBに触れない)。

**方針:取りこぼしより誤検出のほうが有害。** 顧客集中は投資判断(プレモーテム
の反証条件・利食い後の再検討トリガー)に直結するため、疑わしい抽出は拾わない
方を選ぶ。本文抽出では「同一文に customer と % が両方ある」ことを必須条件にし、
XBRL抽出では軸(segment)情報が無いfactを一切採用しない(下の
`extract_from_xbrl` のdocstring参照)。
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

# --- 本文抽出 ---------------------------------------------------------------

# "customer"/"customers" の単語一致(大小文字を問わない)。
_CUSTOMER_WORD_RE = re.compile(r"\bcustomers?\b", re.IGNORECASE)

# 財務諸表の注記に定型的に出る節。"one customer accounted for..." のような
# 具体的な文とセットで使われることが多いが、節そのものにcustomer概念が
# 含まれるため、これも「顧客に関する文」の判定条件に含める。
_CREDIT_RISK_RE = re.compile(r"concentration of credit risk", re.IGNORECASE)

# パーセント表記("23%" "15.4 %" 等)。小数点以下は任意。
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")

# "Customer A" のような、開示側が付けた機械可読に近いラベル。会社によって
# "Customer A"/"Customer 1" 等の表記があるため、英字と数字の両方を許容する。
_NAMED_CUSTOMER_RE = re.compile(r"\bCustomer\s+([A-Z0-9]{1,2})\b")

# 文の終端候補。".!?" の直後に空白+大文字/数字/開き括弧が続く箇所で区切る。
# "U.S." 等の略語で誤って分割することがあるが、下流は「同一文内判定」にしか
# 使わないため、誤って過剰に分割する(=文を短く割る)方が過剰に結合するより
# 安全側(無関係な%を巻き込んで誤検出するリスクを避けられる)。
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


@dataclass(frozen=True)
class ConcentrationMention:
    """本文またはXBRLから抽出した1顧客ぶんの集中度。"""

    customer_label: str
    revenue_pct: float  # 0〜1の小数(23% → 0.23)
    sentence: str  # 根拠原文(人間が確認しに行くための引用)


@dataclass(frozen=True)
class XbrlConcentrationFact:
    """XBRL `ConcentrationRiskPercentage1` の1 factぶん(顧客軸つきの場合のみ)。"""

    customer_label: str
    revenue_pct: float
    period_end: datetime.date
    fiscal_year: int | None
    accession: str | None


def _split_sentences(text: str) -> list[str]:
    collapsed = re.sub(r"\s+", " ", text)
    return [s for s in _SENTENCE_SPLIT_RE.split(collapsed) if s.strip()]


def parse_concentration_text(text: str) -> list[ConcentrationMention]:
    """本文(item1/item7想定)から「1社で売上の10%超」等の開示文を拾う。

    実際の開示文言は多様("one customer accounted for 23% of revenue" /
    "Customer A represented approximately 15.4% of our total revenues" /
    "two customers accounted for 31% and 12%" / "our largest customer
    accounted for 18%")なので、特定の文型に依存せず、**同一文に customer と
    % が両方含まれる**ことだけを必須条件にする(誤検出を避けるための最低限の
    フィルタ)。

    ラベルの決め方:
      - 文中に "Customer A" のような明示ラベルがあり、かつその個数がパーセント
        の個数と一致する場合はそれを使う(出現順に対応付ける)。
      - それ以外は出現順に `customer_1`, `customer_2`, ... という機械ラベルを
        振る("one customer" "two customers" "our largest customer" 等、
        個別名を持たない開示に対応する)。

    `revenue_pct` は 0〜1 の小数で返す(100%を超える値・0以下の値は数値の
    誤爆とみなして除外する)。
    """
    mentions: list[ConcentrationMention] = []
    for sentence in _split_sentences(text):
        has_customer_context = _CUSTOMER_WORD_RE.search(sentence) or _CREDIT_RISK_RE.search(sentence)
        if not has_customer_context:
            continue

        pct_matches = list(_PCT_RE.finditer(sentence))
        if not pct_matches:
            continue

        pct_values = []
        for m in pct_matches:
            value = float(m.group(1)) / 100.0
            if 0 < value <= 1.0:
                pct_values.append(value)
        if not pct_values:
            continue

        named = _NAMED_CUSTOMER_RE.findall(sentence)
        if named and len(named) == len(pct_values):
            labels = [f"Customer {letter}" for letter in named]
        else:
            labels = [f"customer_{i + 1}" for i in range(len(pct_values))]

        clean_sentence = sentence.strip()
        for label, pct in zip(labels, pct_values, strict=True):
            mentions.append(ConcentrationMention(customer_label=label, revenue_pct=pct, sentence=clean_sentence))

    return mentions


# --- XBRL抽出 ----------------------------------------------------------------


def extract_from_xbrl(company_concept_payload: dict) -> list[XbrlConcentrationFact]:
    """`us-gaap:ConcentrationRiskPercentage1` の companyconcept ペイロードから
    顧客別の集中度を取り出す。

    **companyconcept API は軸(segment/dimension)情報を返さない仕様。**
    このタグは本来 `ConcentrationRiskByBenchmarkAxis` のような軸とセットで
    「どの顧客の数値か」を表すが、companyconcept は1タグ×1単位に絞り込んだ
    非次元(non-dimensional)の値だけを返す。つまり fact 単体を見ても、それが
    「会社全体で唯一開示された集中度」なのか「たまたま複数顧客のうち1件だけが
    このAPIに反映された値」なのかを区別できない。

    ここで軸情報の無いfactを「全社合計として1件返す」実装にすると、実際には
    複数顧客に分散しているケースを「1顧客に集中している」と誤って記録しうる
    ——本モジュール冒頭の方針(取りこぼしより誤検出の方が有害)に反する。
    したがって**軸情報が無いfactは常に捨て、空リストを返す**。

    実務上、現行の companyconcept レスポンスに対してこの関数は常に `[]` を
    返す。将来 SEC 側がAPIを拡張してdimensional factを含めるようになった場合
    (または companyfacts 相当のペイロードが渡された場合)に備えて、fact に
    `segment`/`dimensions`/`axis` のいずれかのキーがあれば処理するように
    しておく——companyconcept が変わらない限り無害な予防線に過ぎない。
    """
    facts: list[XbrlConcentrationFact] = []
    units = company_concept_payload.get("units") or {}
    for unit_facts in units.values():
        if not isinstance(unit_facts, list):
            continue
        for fact in unit_facts:
            if not isinstance(fact, dict):
                continue
            axis_info = fact.get("segment") or fact.get("dimensions") or fact.get("axis")
            if not axis_info:
                continue  # 軸が無い = どの顧客の数値か判別不能。採用しない。

            val = fact.get("val")
            end = fact.get("end")
            if val is None or end is None:
                continue
            try:
                pct = float(val)
                period_end = datetime.date.fromisoformat(end)
            except (TypeError, ValueError):
                continue
            if not (0 < pct <= 1.0):
                continue

            label = None
            if isinstance(axis_info, dict):
                label = axis_info.get("member") or axis_info.get("value")
            if not label:
                label = str(axis_info)

            facts.append(
                XbrlConcentrationFact(
                    customer_label=str(label),
                    revenue_pct=pct,
                    period_end=period_end,
                    fiscal_year=fact.get("fy"),
                    accession=fact.get("accn"),
                )
            )
    return facts


# --- 年度をまたいだ低下/消失判定 ---------------------------------------------


@dataclass(frozen=True)
class ConcentrationDropResult:
    triggered: bool
    reason: str | None  # "disclosure_disappeared" / "pct_dropped" / None
    previous_period: datetime.date | None
    previous_max_pct: float | None
    current_period: datetime.date | None
    current_max_pct: float | None


def concentration_drop(
    history: list[tuple[datetime.date, float | None]],
    *,
    disclosure_threshold: float = 0.10,
    drop_threshold_points: float = 0.05,
) -> ConcentrationDropResult:
    """直近2期を比較し、「10%超顧客の開示が消えた」または「最大顧客比率が
    絶対値で `drop_threshold_points` 以上落ちた」かを判定する。

    `history` は `(決算期末, その期に開示された最大顧客集中度)` のリスト。
    `None` は「その期のフィリングは処理したが、10%超の顧客開示が無かった」
    ことを表す(=「まだ処理していない」とは区別する。呼び出し側は 10-K を
    見つけられなかった期をこのリストに含めてはならない——それは「データが
    無い」であって「開示が消えた」ではない)。

    しきい値は両方とも引数で上書き可能(モニタリング指標の
    `MonitoringThresholds` と同じ思想:固定値をハードコードしない)。
    2期に満たない場合は判定不能として `triggered=False` を返す。
    """
    ordered = sorted(history, key=lambda h: h[0])
    if len(ordered) < 2:
        return ConcentrationDropResult(
            triggered=False,
            reason=None,
            previous_period=None,
            previous_max_pct=None,
            current_period=None,
            current_max_pct=None,
        )

    previous_period, previous_pct = ordered[-2]
    current_period, current_pct = ordered[-1]

    triggered = False
    reason: str | None = None
    if previous_pct is not None and previous_pct >= disclosure_threshold and (
        current_pct is None or current_pct < disclosure_threshold
    ):
        triggered = True
        reason = "disclosure_disappeared"
    elif (
        previous_pct is not None
        and current_pct is not None
        and (previous_pct - current_pct) >= drop_threshold_points
    ):
        triggered = True
        reason = "pct_dropped"

    return ConcentrationDropResult(
        triggered=triggered,
        reason=reason,
        previous_period=previous_period,
        previous_max_pct=previous_pct,
        current_period=current_period,
        current_max_pct=current_pct,
    )
