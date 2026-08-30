"""Tier 2(監視対象リスト)の判定ロジック(15.5「二層構成」)。

15.5はランキングを二層に分けることを求めている:

> Tier 1(全ゲート通過・カバレッジ高)と Tier 2(監視対象:ゲート1つ未達 or
> データ不足)を分離表示。IPO直後銘柄(10章)もTier 2に置けば、除外せずに
> 追跡できる

Tier 1 は `GET /candidates`(P(10x) 降順ランキング)がそのまま担う。
本モジュールは **「メインのランキングには出てこないが、追跡する価値がある銘柄」**
を理由つきで洗い出す。

**`high_growth_suppressed`(高成長だが総合スコアが伸びない)を削除した経緯**:
この分類は26章で、加重幾何平均が10バガーの典型プロファイルを構造的に沈める
という**モデル側の欠陥に対する対症療法**として追加したものだった。27章で
スコアリングを実現時価総額倍率モデルに置き換え、高成長銘柄が沈む原因
(パーセンタイル化と幾何平均)そのものを取り除いたため、この救済措置は
不要になった。成長が突出した銘柄は今や Tier 1 の上位に直接現れる。

残る3分類はいずれも「モデルの外側の理由でランキングに載らない銘柄」であり、
モデルを差し替えても意味を保つ。

すべて純粋関数として実装する(DBアクセスを持たない)。呼び出し元(API層)が
universe_snapshots・scores から値を集めて渡す。
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Tier 2 に載せる理由 -------------------------------------------------------

SINGLE_GATE_MISS = "single_gate_miss"
RECENT_LISTING = "recent_listing"
INSUFFICIENT_DATA = "insufficient_data"
NEGATIVE_OUTLOOK = "negative_outlook"

REASON_LABELS: dict[str, str] = {
    SINGLE_GATE_MISS: "ゲート1つ未達",
    RECENT_LISTING: "新規上場ウォッチリスト",
    INSUFFICIENT_DATA: "データ不足でスコア算出不能",
    NEGATIVE_OUTLOOK: "見通しがマイナス",
}

# 「あと一歩」として監視する価値がある除外理由。
#
# 時価総額・売上高の上限超過とセクター除外を含めないのは、これらが
# **改善して復帰することを期待する類の条件ではない**ため(15.6:大きすぎる企業は
# 算数上10倍になれない。セクターは変わらない)。仮に縮小して基準内に戻ったと
# しても、それは監視の成果ではなく事業の悪化を意味する。
#
# `missing_*`・`no_raw_data` も含めない。これらは「あと一歩」ではなく
# 「判定に必要なデータが無く何も言えない」状態であり、監視リストに混ぜると
# ノイズになる(既存の `GET /excluded` で確認できる)。
WATCHABLE_SINGLE_GATES: frozenset[str] = frozenset(
    {
        "dilution_ceiling",
        "cash_runway_floor",
        "liquidity_floor",
        "price_floor",
        "negative_equity",
        "insufficient_listing_history",
    }
)

GATE_DETAIL_LABELS: dict[str, str] = {
    "dilution_ceiling": "希薄化率が上限超過(他のゲートはすべて通過)",
    "cash_runway_floor": "キャッシュランウェイが下限未満(他のゲートはすべて通過)",
    "liquidity_floor": "売買代金が下限未満(他のゲートはすべて通過)",
    "price_floor": "株価が下限未満(他のゲートはすべて通過)",
    "negative_equity": "自己資本がマイナス(他のゲートはすべて通過)",
    "insufficient_listing_history": "決算データの期数が不足(4章:上場後最低4四半期)",
}


@dataclass(frozen=True)
class GateOutcome:
    """その日のゲート判定結果。"""

    ticker_id: int
    included: bool
    exclusion_reasons: list[str]


@dataclass(frozen=True)
class Tier2Entry:
    ticker_id: int
    reason: str
    detail: str
    # 未達だったゲート名(`single_gate_miss`・`recent_listing` のときのみ)。
    # UI側で「流動性だけが足りない銘柄」のように絞り込めるようにするため、
    # 表示用の文章(detail)とは別に機械可読な形でも持たせる。
    gate: str | None = None

    def sort_key(self) -> tuple[str, str]:
        """理由ごとにまとめる。理由の中の順序は呼び出し元がティッカー順で安定させる。"""
        return (self.reason, self.gate or "")


def classify_excluded(gate: GateOutcome) -> tuple[str, str] | None:
    """除外された銘柄がTier 2(監視対象)に該当するかを判定する。

    ゲートを1つだけ落としており、かつそれが「改善して復帰しうる」種類の
    ゲートであれば監視対象とする(`WATCHABLE_SINGLE_GATES` 参照)。
    """
    if gate.included or len(gate.exclusion_reasons) != 1:
        return None
    reason = gate.exclusion_reasons[0]
    if reason not in WATCHABLE_SINGLE_GATES:
        return None
    detail = GATE_DETAIL_LABELS.get(reason, reason)
    # 10章:IPO直後銘柄は「除外せずに追跡する」ことが15.5で明示されているため、
    # 他のゲート未達とは別ラベルにして新規上場ウォッチリストとして扱う。
    if reason == "insufficient_listing_history":
        return RECENT_LISTING, detail
    return SINGLE_GATE_MISS, detail


def build_tier2(
    gates: list[GateOutcome],
    ranked_ticker_ids: set[int],
    negative_outlook_ticker_ids: set[int] | None = None,
) -> list[Tier2Entry]:
    """その日の Tier 2(監視対象)を組み立てる。

    - 除外されたが「ゲート1つ未達」の銘柄(15.5)
    - IPO直後で決算実績が足りない銘柄(10章・15.5)
    - ゲートは通過したが**測れなかった**銘柄
    - ゲートは通過し測れたが、**中心的な見通しがマイナス**の銘柄(27.17)

    最後の2つを分けるのは、実データでランキング外の71%が後者だからである。
    両方を「データ不足」と表示すると、**測った結果を測れなかったことにして
    しまう**——期待倍率0.7倍という事実は、データが無いことより遥かに有用な情報。
    """
    negative_outlook_ticker_ids = negative_outlook_ticker_ids or set()
    entries: list[Tier2Entry] = []

    for gate in gates:
        classified = classify_excluded(gate)
        if classified is not None:
            reason, detail = classified
            entries.append(
                Tier2Entry(
                    ticker_id=gate.ticker_id, reason=reason, detail=detail, gate=gate.exclusion_reasons[0]
                )
            )
            continue
        if not gate.included:
            continue

        # 27.17・27.20:ゲートを通過してもランキングに載らない理由は2つある。
        # どちらも**低いスコアを付けずにここへ回す**——欠損を減点に読み替えると
        # 「データが無い」が「悪い銘柄」として誤読されるし、見通しマイナスの
        # 銘柄に順位を付けると、ばらつきの大きさだけを理由に上位へ来てしまう。
        if gate.ticker_id in negative_outlook_ticker_ids:
            entries.append(
                Tier2Entry(
                    ticker_id=gate.ticker_id,
                    reason=NEGATIVE_OUTLOOK,
                    detail=(
                        "モデルは算出できたが、期待倍率が1.0を下回る"
                        "(売上成長・利益率・マルチプル・希薄化を7年後まで外挿すると、"
                        "中心的な見通しで株主価値を毀損する)。事業が回復すれば候補に戻る"
                    ),
                )
            )
        elif gate.ticker_id not in ranked_ticker_ids:
            entries.append(
                Tier2Entry(
                    ticker_id=gate.ticker_id,
                    reason=INSUFFICIENT_DATA,
                    detail=(
                        "全ゲートを通過したが、実現倍率モデルの必須入力"
                        "(開示済み年次売上2期・粗利・発行済株式数・株価)が揃わないか、"
                        "株式がEVのごく一部しか占めない超高レバレッジでモデルが成立しない"
                    ),
                )
            )

    return entries
