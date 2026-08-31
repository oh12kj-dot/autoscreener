"""Form 4(インサイダー売買)の取得・解析(docs/defect_and_edge_audit_2026-08-28.md I-3)。

**なぜ効くか**:経営陣は自社の四半期先を最もよく知っている。小型株での
インサイダー買いは、公開情報の中で最も頑健に文書化されたリターン予測因子の
1つであり、**プロのファンドはマイクロキャップの Form 4 を体系的に使いにくい**
(規模制約とコンプライアンス)。構造優位そのもの。

**モデルへの入れ方(手順を守ること)**:
1. まず `MoicInputs` に**入れない**。`Observation` の診断フィールドとして保存し、
   `run-backtest` で **モデル確率の上位デシルに条件付けた順位IC**を測る
   (28.10 の Piotroski と同じ手続き)。
2. IC が有意なら `growth_fade` 経路か独立項として `mu` に加算。`health_index` は
   **間違い**(インサイダー買いは生存確率ではなくリターンの情報)。
3. 指標は「直近6ヶ月の net open-market buying(P − S)を時価総額で正規化」。
   **金額ではなく比率**(絶対額は時価総額と相関する)。

このモジュールは Form 4 XML(`ownershipDocument`)のパースと、そこから
`InsiderSignal` を組み立てるところまで。取得(full-index 走査 + 文書取得)は
`EdgarClient` を使う薄いオーケストレーションで、ネットワークが要る。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from lxml import etree

# transactionCode: P=市場買付, S=売却, A=付与, M=オプション行使, F=税金納付のための源泉,
# G=贈与, C=転換, X=行使。買いシグナルとして数えるのは市場での自発的買付(P)のみ。
OPEN_MARKET_BUY = "P"
OPEN_MARKET_SELL = "S"


@dataclass(frozen=True)
class Form4Transaction:
    transaction_date: datetime.date
    code: str
    shares: float
    price_per_share: float | None
    acquired: bool  # A=取得, D=処分
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    officer_title: str | None
    shares_owned_following: float | None


@dataclass(frozen=True)
class InsiderSignal:
    """1銘柄・ある基準日から遡って6ヶ月の集計。"""

    as_of: datetime.date
    net_open_market_buy_shares: float  # Σ買付株数 − Σ売却株数(市場取引のみ)
    net_open_market_buy_usd: float
    buy_transaction_count: int
    sell_transaction_count: int
    distinct_buyers: int

    def normalized_by_market_cap(self, market_cap: float | None) -> float | None:
        if not market_cap or market_cap <= 0:
            return None
        return self.net_open_market_buy_usd / market_cap


def _text(node, path: str) -> str | None:
    found = node.find(path)
    if found is None:
        return None
    # 値は <value> 子要素に入ることが多い(footnote 付き)。
    value = found.find("value")
    raw = (value.text if value is not None else found.text) or ""
    return raw.strip() or None


def _float(node, path: str) -> float | None:
    raw = _text(node, path)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_form4(xml: bytes | str) -> list[Form4Transaction]:
    """Form 4 の `ownershipDocument` XML から非デリバティブ取引を取り出す。"""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError:
        return []

    owner = root.find("reportingOwner/reportingOwnerRelationship")
    is_director = (_text(owner, "isDirector") or "0") in ("1", "true")
    is_officer = (_text(owner, "isOfficer") or "0") in ("1", "true")
    is_ten = (_text(owner, "isTenPercentOwner") or "0") in ("1", "true")
    officer_title = _text(owner, "officerTitle") if owner is not None else None

    transactions: list[Form4Transaction] = []
    for txn in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        date_raw = _text(txn, "transactionDate")
        if not date_raw:
            continue
        try:
            txn_date = datetime.date.fromisoformat(date_raw)
        except ValueError:
            continue
        amounts = txn.find("transactionAmounts")
        coding = txn.find("transactionCoding")
        code = _text(coding, "transactionCode") if coding is not None else None
        shares = _float(amounts, "transactionShares") if amounts is not None else None
        price = _float(amounts, "transactionPricePerShare") if amounts is not None else None
        acq_disp = _text(amounts, "transactionAcquiredDisposedCode") if amounts is not None else None
        post = txn.find("postTransactionAmounts")
        owned_following = _float(post, "sharesOwnedFollowingTransaction") if post is not None else None
        if code is None or shares is None:
            continue
        transactions.append(
            Form4Transaction(
                transaction_date=txn_date,
                code=code,
                shares=shares,
                price_per_share=price,
                acquired=(acq_disp == "A"),
                is_director=is_director,
                is_officer=is_officer,
                is_ten_percent_owner=is_ten,
                officer_title=officer_title,
                shares_owned_following=owned_following,
            )
        )
    return transactions


def build_insider_signal(
    transactions: list[Form4Transaction],
    as_of: datetime.date,
    lookback_days: int = 183,
) -> InsiderSignal:
    """`as_of` から `lookback_days` 遡った窓の net open-market buying を集計する。

    先読みは原理的に起きない——Form 4 は取引から2営業日以内の提出が義務。
    ただし呼び出し元は「提出日 <= as_of」で事前に絞ること(このモジュールは
    取引日で窓を切るだけ)。
    """
    window_start = as_of - datetime.timedelta(days=lookback_days)
    buy_shares = sell_shares = 0.0
    buy_usd = 0.0
    buy_n = sell_n = 0
    buyers: set[tuple] = set()
    for txn in transactions:
        if not (window_start <= txn.transaction_date <= as_of):
            continue
        if txn.code == OPEN_MARKET_BUY and txn.acquired:
            buy_shares += txn.shares
            buy_usd += txn.shares * (txn.price_per_share or 0.0)
            buy_n += 1
            buyers.add((txn.officer_title, txn.is_director, txn.is_officer))
        elif txn.code == OPEN_MARKET_SELL and not txn.acquired:
            sell_shares += txn.shares
            sell_n += 1
    return InsiderSignal(
        as_of=as_of,
        net_open_market_buy_shares=buy_shares - sell_shares,
        net_open_market_buy_usd=buy_usd,
        buy_transaction_count=buy_n,
        sell_transaction_count=sell_n,
        distinct_buyers=len(buyers),
    )
