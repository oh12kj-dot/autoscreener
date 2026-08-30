"""10-K/10-Q本文をItem単位に切り出す純関数群(K-2)。

`research/TEMPLATE.md` の顧客集中(`customer_concentration_disclosed_drop`)を
はじめ、下流の本文解析(K-3の顧客集中、将来の訴訟・希薄化条項の抽出)は
すべてここが切り出した `item1` / `item1a` / `item3` / `item7` を読むだけで
完結させる。EDGARへの再アクセスを増やさないため、ここはネットワークに
一切触らない——`EdgarClient.fetch_document_text()` が返したプレーンテキスト
を受け取るだけの純関数にする(テストしやすさと再利用性のため)。

**TOC(目次)問題への対処方針**:10-K/10-Qは冒頭に目次があり、"Item 1A."
のような見出し文字列は本文の見出しとTOCの両方に、さらに本文中の相互参照
("as described in Item 1A. Risk Factors above")にも出現する。**最後の出現を
採る**という単純な方式は、TOCが末尾に近い(稀に索引が巻末にある)場合や、
本文の最後の方で他セクションへの相互参照がある場合に誤爆する。

代わりに、同じ見出し(例: "item1a")の**全出現**について「その出現位置から、
次に出てくる何らかのItem見出しまでの距離」を計算し、**最も長い区間を生む
出現を採用する**。理由:
  - TOCでは見出しが数行おきに密集して並ぶため、区間は必ず短い。
  - 本文中の相互参照は前後にすぐ次の文・次の見出しが来ることが多く、
    実際のセクション本文(数千〜数万文字)に比べて区間が短い。
  - 本物のセクション見出しの直後には、次のItemが始まるまで長い本文が続く。
以上より「区間が最長」は「本物の本文セクション」に対する実用上十分な代理
指標になる。完全ではない(相互参照の直後に他の見出しが長らく現れない病的な
文書では誤爆しうる)が、取りこぼしより誤検出を避けたい下流(顧客集中の
判定等)に対しては、短い区間を捨てる本方式の方が安全側に倒れる。
"""

from __future__ import annotations

import re
import unicodedata

# Item見出しの正規表現。"item"の前後に他の英字が続く場合(subitem等)は除外し、
# 数字1〜2桁+任意の1文字(A〜F、1A/1B/1C/7A/9A/9B/9C等をカバー)を拾う。
# 空白は0〜3文字まで許容し、"Item1A"(空白無し)・"Item  1A"(複数空白)・
# "ITEM 1A"(大文字)のいずれにも耐える。大文字小文字は re.IGNORECASE で吸収する。
_HEADING_RE = re.compile(
    r"(?<![A-Za-z])item\s{0,3}(\d{1,2})\s{0,2}([A-Fa-f])?(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# 10-K:キー名がそのまま出力キーになる(下流はformを意識しない)。
_FORM_10K_TARGETS: dict[str, str] = {
    "item1": "item1",
    "item1a": "item1a",
    "item3": "item3",
    "item7": "item7",
}

# 10-Q:Part I Item 1(財務諸表)・Part II Item 1(訴訟)は対象外——10-Kの
# item1/item3とは意味が異なり、混ぜると下流の判定を汚染する。対象は
# Part II Item 1A(リスク要因の更新)とPart I Item 2(MD&A)の2つだけ。
# 出力キーは10-Kと揃える(item2 → item7)ことで、下流が form を分岐せずに
# 同じキーで読めるようにする。
_FORM_10Q_TARGETS: dict[str, str] = {
    "item1a": "item1a",
    "item2": "item7",
}


def _normalize(text: str) -> str:
    """全角英数字・記号をNFKCで半角化し、`&nbsp;`(エンティティ表記のまま
    紛れ込むことがある)と改行混じりの空白を通常の半角スペースに畳む。

    lxmlでHTMLタグを除去した後も `\\xa0`(非改行スペース)がそのまま残る
    ことがあり、見出しの空白幅が不揃いになる原因になるため、ここで統一する。
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("&nbsp;", " ").replace("\xa0", " ")
    return normalized


def _iter_headings(text: str) -> list[tuple[str, int, int]]:
    """`(正規化キー, 開始位置, 終了位置)` のリスト。出現順(=文書中の位置順)。"""
    headings: list[tuple[str, int, int]] = []
    for m in _HEADING_RE.finditer(text):
        number = m.group(1)
        letter = (m.group(2) or "").lower()
        key = f"item{number}{letter}"
        headings.append((key, m.start(), m.end()))
    return headings


def split_sections(text: str, form: str) -> dict[str, str]:
    """10-K/10-Q本文をItem単位に切り出す。

    戻り値のキーは `item1` / `item1a` / `item3` / `item7`(10-Qは `item1a` /
    `item7` の2つのみ)。**切り出せなかったセクションはキーごと返さない**
    (空文字を入れない)。これは `Filing.analysis` と同じ方針——「解析したが
    無かった」(キーが無い)と「解析していない」(呼び出し自体をしていない)を
    下流が区別できるようにするため。

    `form` が "10-K"/"10-Q"(および "10-K/A"等の修正版)のいずれでもない場合は
    空dictを返す(8-K等はこの関数の対象外——`ex99`はItem番号を持たない添付
    なので、呼び出し側が別経路で保存する)。
    """
    form_upper = form.upper()
    if form_upper.startswith("10-Q"):
        targets = _FORM_10Q_TARGETS
    elif form_upper.startswith("10-K"):
        targets = _FORM_10K_TARGETS
    else:
        return {}

    normalized = _normalize(text)
    headings = _iter_headings(normalized)
    if not headings:
        return {}

    all_starts = [start for _key, start, _end in headings]

    result: dict[str, str] = {}
    for source_key, output_key in targets.items():
        candidates = [(start, end) for key, start, end in headings if key == source_key]
        if not candidates:
            continue

        best_start: int | None = None
        best_end: int | None = None
        best_length = -1
        for start, end in candidates:
            later_starts = [s for s in all_starts if s > start]
            section_end = min(later_starts) if later_starts else len(normalized)
            length = section_end - end
            if length > best_length:
                best_length = length
                best_start = end
                best_end = section_end

        if best_start is None or best_end is None:
            continue
        section_text = normalized[best_start:best_end].strip()
        if section_text:
            result[output_key] = section_text

    return result
