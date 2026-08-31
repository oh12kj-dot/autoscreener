"""Form 4(インサイダー取引)の実取得(K-7:docs/defect_and_edge_audit_2026-08-28.md I-3 の配線)。

`collectors/form4_source.py` にパース本体(`parse_form4` / `build_insider_signal`)
は既に実装済み。このモジュールはそこへ実データを流し込む薄いオーケストレーション
だけを担う——`EdgarClient.fetch_filings` で Form 4 の提出一覧を取り、各提出の
生XMLを `fetch_raw` で取得して `parse_form4` に渡し、`batch/collect_supply.py`
の `InsiderRow` に変換する。

**EDGARの罠(必ず踏まえること)**:1件のForm 4提出には、機械可読な生XML
(例 `wf-form4_1234567890.xml`)と、人間可読なXSL変換済みHTML
(例 `xslF345X03/wf-form4_1234567890.xml` ——**拡張子は.xmlだがXSLTを当てた
レンダリング結果でありHTMLに近い**)の両方が同じ `index.json` に並ぶ。
後者を `lxml.etree` に渡すと `ownershipDocument` 構造が無くパースが
実質空振りになる。`_select_xml_document` でファイル名に "xsl" を含む
ものを除外して前者だけを拾う。

**循環importについての設計判断**:`batch/collect_supply.py` はこのモジュールを
import して既定fetcherとして使う。逆に本モジュールが `collect_supply` を
モジュールレベルでimportすると循環importになる。ここでは
「`InsiderRow` の型ヒントは `TYPE_CHECKING` の下だけで見せ、実際の
importは関数呼び出し時に遅延させる」方式を選んだ(`collect_supply` が
先に読み込まれてから `fetch_form4_rows` が呼ばれる前提が常に成り立つため
安全)。もう一つの選択肢(本モジュール側で同型のdataclassを独自定義し
`collect_supply` 側で変換する)よりこちらを選んだ理由は、変換の重複や
フィールド追従漏れのリスクを避け、`InsiderRow` の定義を1箇所に保つため。
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from lxml import etree

from autoscreener.collectors.edgar_client import EdgarClient, FilingRecord, filing_file_url
from autoscreener.collectors.errors import CollectionError
from autoscreener.collectors.form4_source import Form4Transaction, parse_form4
from autoscreener.dates import utc_today

if TYPE_CHECKING:
    from autoscreener.batch.collect_supply import InsiderRow

logger = logging.getLogger(__name__)

# 既定の遡及窓(約18か月)。インサイダーシグナルは `build_insider_signal` が
# 直近6か月で集計するが、取得側はもう少し広めに持っておく
# (時系列表示・将来のバックテスト再現のため)。
_DEFAULT_LOOKBACK_DAYS = 548

# EDGARのXSL変換版(レンダリング用HTML)を除外するためのマーカー。
# 例: "xslF345X03/wf-form4_....xml"。生XMLのファイル名にはこの文字列が
# 含まれない(モジュールdocstring参照)。
_XSL_MARKER = "xsl"


def _select_xml_document(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """提出書類のファイル一覧(`fetch_filing_index` の戻り値)から生XML本体を選ぶ。

    `.xml` で終わり、かつファイル名に "xsl" を含まないものだけを候補にする。
    複数候補が残る場合(稀)はファイル名でソートして先頭を採用し、挙動を
    決定的にする。見つからなければ None(呼び出し側はこの提出をスキップする)。
    """
    candidates = [
        item
        for item in items
        if isinstance(item.get("name"), str)
        and item["name"].lower().endswith(".xml")
        and _XSL_MARKER not in item["name"].lower()
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["name"])
    return candidates[0]


def _owner_name(xml_text: str) -> str | None:
    """`reportingOwner/reportingOwnerId/rptOwnerName` を取り出す。

    `form4_source.parse_form4` は取引明細と関係フラグ(is_director等)しか
    返さず、提出者氏名フィールドを持たない——共有パーサであり本タスクでは
    改変しない方針のため、氏名だけはここで個別に読み取る。`parse_form4` と
    同じく最初の `reportingOwner` のみを見る(共同提出の場合も
    `Form4Transaction` 側は区別しないため、合わせておく)。
    """
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except etree.XMLSyntaxError:
        return None
    node = root.find("reportingOwner/reportingOwnerId/rptOwnerName")
    if node is None or not node.text:
        return None
    return node.text.strip() or None


def _role_label(txn: Form4Transaction) -> str | None:
    """`insider_transactions.role` に入れる短い肩書き文字列。優先順位は
    役員(肩書きがあればそれ、無ければ"Officer") > 取締役 > 10%株主。"""
    if txn.is_officer and txn.officer_title:
        return txn.officer_title
    if txn.is_officer:
        return "Officer"
    if txn.is_director:
        return "Director"
    if txn.is_ten_percent_owner:
        return "10% Owner"
    return None


def fetch_form4_rows(
    client: EdgarClient,
    cik: str,
    *,
    since: datetime.date | None = None,
    max_filings: int = 40,
) -> list[InsiderRow]:
    """指定CIKのForm 4提出を新しい順に辿り、`InsiderRow` のリストへ変換する。

    ネットワークI/O(EDGAR)を行う。1提出の取得・パース失敗
    (`CollectionError`)は握って次の提出へ進む——`batch/collect_filings.py`
    と同じく、1件の失敗で銘柄全体の収集を止めないため。
    レート制御は `EdgarClient` が内部で持っているため、ここでは sleep しない。
    """
    from autoscreener.batch.collect_supply import InsiderRow  # 遅延import(モジュールdocstring参照)

    cutoff = since or (utc_today() - datetime.timedelta(days=_DEFAULT_LOOKBACK_DAYS))

    try:
        filings = client.fetch_filings(cik, forms={"4"})
    except CollectionError:
        logger.warning("CIK %s: Form4 提出一覧の取得に失敗", cik, exc_info=True)
        return []

    # 新しい順に並べ、filed_date による事前フィルタでネットワーク往復を削る
    # (取引は原則提出の2営業日以内——form4_source.pyのdocstring参照)。
    # 最終的な採否は取引日そのもので判定する(下の _rows_for_filing)。
    targets = sorted(
        (f for f in filings if f.filed_date >= cutoff),
        key=lambda f: f.filed_date,
        reverse=True,
    )[:max_filings]

    rows: list[InsiderRow] = []
    for filing in targets:
        rows.extend(_rows_for_filing(client, cik, filing, cutoff, InsiderRow))
    return rows


def _rows_for_filing(
    client: EdgarClient,
    cik: str,
    filing: FilingRecord,
    cutoff: datetime.date,
    insider_row_cls: type[InsiderRow],
) -> list[InsiderRow]:
    try:
        items = client.fetch_filing_index(cik, filing.accession_number)
    except CollectionError:
        logger.warning(
            "CIK %s accession %s: ファイル一覧の取得に失敗", cik, filing.accession_number, exc_info=True
        )
        return []

    xml_item = _select_xml_document(items)
    if xml_item is None:
        logger.warning(
            "CIK %s accession %s: 生XML(非XSL)の添付が見つからない", cik, filing.accession_number
        )
        return []

    url = filing_file_url(cik, filing.accession_number, xml_item["name"])
    try:
        xml_text = client.fetch_raw(url)
    except CollectionError:
        logger.warning("CIK %s accession %s: XML取得に失敗", cik, filing.accession_number, exc_info=True)
        return []

    transactions = parse_form4(xml_text)
    if not transactions:
        return []
    owner_name = _owner_name(xml_text) or "(unknown)"

    rows: list[InsiderRow] = []
    for txn in transactions:
        if txn.transaction_date < cutoff:
            continue
        value_usd = txn.shares * txn.price_per_share if txn.price_per_share is not None else None
        rows.append(
            insider_row_cls(
                accession_number=filing.accession_number,
                transaction_date=txn.transaction_date,
                insider_name=owner_name,
                transaction_code=txn.code,
                shares=txn.shares,
                filed_date=filing.filed_date,
                role=_role_label(txn),
                price_usd=txn.price_per_share,
                value_usd=value_usd,
                is_derivative=False,  # parse_form4はnonDerivativeTransactionのみ扱う
            )
        )
    return rows
