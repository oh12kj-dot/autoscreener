"""ティッカーシンボルの表記ゆれ吸収(30.2.1・30.3.2)。

クラス株のドット表記(``BRK.B``)とハイフン表記(``BRK-B``)は、データ源ごとに
どちらを使うかが違う——NASDAQ Trader由来の `tickers.symbol` はハイフン、
利用者が証券会社サイトから貼り付けるリストやSECの `company_tickers.json` は
ドットのことがある。**この吸収ロジックを1箇所にまとめる**のが目的で、
`screening/tradability.py`(取扱可否の突合)と `batch/refresh_cik_map.py`
(SEC CIKの突合)の両方がここを参照する。同じ正規化を2箇所に書くと、
どちらか一方だけが後から直されて突合漏れが再発する。
"""

from __future__ import annotations


def normalize_symbol(symbol: str) -> str:
    """比較の基準形にする:前後の空白除去・大文字化のみ。"""
    return symbol.strip().upper()


def symbol_variants(symbol: str) -> frozenset[str]:
    """ある1つのシンボル表記から、同一銘柄を指しうる表記のバリエーションを返す。

    ``.`` と ``-`` を相互に読み替えるだけで足りる——優先株の "$" 等その他の記号は
    データ源間で綴りが揺れないため対象にしない。基準形自身も必ず含む。
    """
    base = normalize_symbol(symbol)
    variants = {base}
    if "." in base:
        variants.add(base.replace(".", "-"))
    if "-" in base:
        variants.add(base.replace("-", "."))
    return frozenset(variants)
