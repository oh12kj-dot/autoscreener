"""希薄化キャパシティの本文抽出(K-4)。

`research/TEMPLATE.md` の `dilution:` ブロック(`remaining_shelf_capacity_usd` /
`atm_remaining_usd` / `unexercised_options_ratio` / `has_variable_conversion_price`)
はこれまで人間が S-3 / 424B5 / 10-Q を読んで手入力していた。ここではその本文
テキストから金額・条項を抜き出す**純関数**だけを置く(`dilution_outlook.py` と
同じ方針:ネットワーク・DBに一切触れない)。

**「本文中で最初に出てきた $ 金額」を取るのは誤り。** S-3 の表紙には引受手数料
テーブル・1株あたり価格・別の証券種別の金額など、無関係な $ 表記が並ぶ。この
モジュールは常に「金額の近傍(既定で前後100〜300字)に手がかりとなる語句が
あるか」で候補を絞り込む方式を取る。

金額文字列のパースは `parse_usd_amount` に一本化する(`$150.0 million` /
`$1.2 billion` / カンマ区切り / `$150,000,000(1)` のような脚注記号のいずれにも
耐える——脚注記号は数値パターンにマッチしないので、パースを試みる前の時点で
自然に無視される)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SCALE_MULTIPLIERS: dict[str, float] = {
    "thousand": 1_000.0,
    "million": 1_000_000.0,
    "billion": 1_000_000_000.0,
}

# `$` + 数字(カンマ区切り可・小数可) + 任意の位取り接尾辞。
# 脚注記号(例 "(1)")はこのパターンの外なので、末尾に付いていても無視される。
_AMOUNT_TOKEN_PATTERN = re.compile(
    r"\$\s?[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:million|billion|thousand)?",
    re.IGNORECASE,
)
_NUMBER_PATTERN = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")
_SCALE_WORD_PATTERN = re.compile(r"million|billion|thousand", re.IGNORECASE)


def parse_usd_amount(text: str) -> float | None:
    """`text` 中で最初に見つかった `$` 金額表記1個を float に変換する共通ヘルパ。

    対応する表記:
    - ``"$150,000,000"``(カンマ区切り整数)
    - ``"$150.0 million"`` / ``"$1.2 billion"``(位取り接尾辞)
    - ``"$150,000,000(1)"``(脚注記号つき。脚注番号は数値パターンの外なので
      そのまま無視される)

    見つからなければ None。
    """
    match = _AMOUNT_TOKEN_PATTERN.search(text)
    if match is None:
        return None
    return _amount_from_token(match.group(0))


def _amount_from_token(token: str) -> float | None:
    num_match = _NUMBER_PATTERN.search(token)
    if num_match is None:
        return None
    value = float(num_match.group(0).replace(",", ""))
    scale_match = _SCALE_WORD_PATTERN.search(token)
    if scale_match is not None:
        value *= _SCALE_MULTIPLIERS[scale_match.group(0).lower()]
    return value


def _iter_amounts(text: str):
    """`text` 中のすべての `$` 金額表記を (開始位置, 終了位置, 金額) で列挙する。"""
    for m in _AMOUNT_TOKEN_PATTERN.finditer(text):
        value = _amount_from_token(m.group(0))
        if value is not None:
            yield m.start(), m.end(), value


def _find_amount(
    text: str,
    *,
    required_groups: tuple[tuple[re.Pattern[str], ...], ...],
    window: int,
    evidence_window: int = 200,
) -> tuple[float, str] | None:
    """`text` 中の $ 金額のうち、`required_groups` の各グループから少なくとも
    1つのパターンが**金額の手前** `window` 文字以内に現れるものを返す
    (グループ間は AND、グループ内は OR)。

    手前だけを見る(後方は見ない)のは意図的。「aggregate offering price of
    up to $X」「gross proceeds of $Y」のようにSEC提出書類の定型句は必ず
    手がかり語 → 金額の語順で現れる。前後対称の窓にすると、直後の別段落に
    出てくる無関係な語句(例:表紙の希釈対象外の金額のすぐ後に続く次の段落の
    "aggregate offering price")まで拾ってしまい、誤検出の原因になる
    (実装時にテストで実際に踏んだ失敗パターン)。

    最初に条件を満たした金額を採用する。
    """
    for start, end, value in _iter_amounts(text):
        ctx_start = max(0, start - window)
        ctx = text[ctx_start:start]
        if all(any(p.search(ctx) for p in group) for group in required_groups):
            ev_start = max(0, start - evidence_window)
            ev_end = min(len(text), end + evidence_window)
            return value, text[ev_start:ev_end].strip()
    return None


# --- シェルフ登録額(S-3 / S-3ASR の表紙) --------------------------------------

_SHELF_TRIGGER_PATTERNS = (
    re.compile(r"aggregate\s+offering\s+price", re.IGNORECASE),
    re.compile(r"aggregate\s+principal\s+amount", re.IGNORECASE),
    re.compile(r"\bup\s+to\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class ShelfCapacity:
    amount_usd: float
    evidence: str


def parse_shelf_capacity(text: str) -> ShelfCapacity | None:
    """S-3 / S-3ASR の表紙にある総登録額(シェルフ枠)を抜く。

    表紙には引受手数料表(1株あたり $X)や別証券種別の金額も出てくるため、
    「aggregate offering price」「aggregate principal amount」「up to」の
    いずれかが金額の近傍(既定100字以内)にあるものだけを候補にする。
    """
    result = _find_amount(text, required_groups=(_SHELF_TRIGGER_PATTERNS,), window=120)
    if result is None:
        return None
    amount, evidence = result
    return ShelfCapacity(amount_usd=amount, evidence=evidence)


# --- ATM(at-the-market)残枠 ---------------------------------------------------

_ATM_WORD_PATTERNS = (
    re.compile(r"at-the-market", re.IGNORECASE),
    re.compile(r"at\s+the\s+market", re.IGNORECASE),
    re.compile(r"\bATM\b"),
)
_ATM_CAPACITY_WORD_PATTERNS = (
    re.compile(r"\bup\s+to\b", re.IGNORECASE),
    re.compile(r"aggregate\s+offering\s+price", re.IGNORECASE),
    re.compile(r"authorized", re.IGNORECASE),
)
_ATM_PROCEEDS_WORD_PATTERNS = (
    re.compile(r"gross\s+proceeds", re.IGNORECASE),
    re.compile(r"net\s+proceeds", re.IGNORECASE),
)


@dataclass(frozen=True)
class AtmCapacity:
    """ATMプログラムの授権額と消化済み額。`remaining_usd` = 授権額 − 消化済み額。

    どちらか一方しか本文から取れないこともあるので、`remaining_usd` は
    両方が揃ったときだけ計算する(片方だけで差し引くと過大な残枠を報告する)。
    """

    authorized_usd: float | None
    sold_usd: float | None
    remaining_usd: float | None
    evidence: dict[str, str]


def parse_atm_capacity(text: str) -> AtmCapacity | None:
    """`at-the-market` / ATM プログラムの授権額と、10-Q等の消化実績(gross
    proceeds)を抜く。

    授権額は「ATM を示す語」と「up to / aggregate offering price / authorized
    のいずれか」の両方が金額の近傍にあることを条件にする(単に ATM に言及した
    段落の別の金額を誤って拾わないため)。消化額は「ATM を示す語」と
    「gross/net proceeds」の両方を条件にする。
    """
    authorized = _find_amount(
        text, required_groups=(_ATM_WORD_PATTERNS, _ATM_CAPACITY_WORD_PATTERNS), window=300
    )
    sold = _find_amount(
        text, required_groups=(_ATM_WORD_PATTERNS, _ATM_PROCEEDS_WORD_PATTERNS), window=300
    )
    if authorized is None and sold is None:
        return None

    authorized_usd = authorized[0] if authorized else None
    sold_usd = sold[0] if sold else None
    remaining_usd = (
        authorized_usd - sold_usd if authorized_usd is not None and sold_usd is not None else None
    )
    evidence: dict[str, str] = {}
    if authorized is not None:
        evidence["authorized"] = authorized[1]
    if sold is not None:
        evidence["sold"] = sold[1]

    return AtmCapacity(
        authorized_usd=authorized_usd,
        sold_usd=sold_usd,
        remaining_usd=remaining_usd,
        evidence=evidence,
    )


# --- 変動転換価格(デススパイラル転換社債)の検知 -------------------------------

# いわゆる「デススパイラル」型の転換価格条項。投資判断上ほぼ即死要因なので、
# 検出したら必ず根拠原文を返す(数字を出さないアラートは人間に原本を
# 読み直させることになり、自動化した意味が消える)。
_VARIABLE_CONVERSION_PATTERNS = (
    re.compile(
        r"conversion\s+price[^.]{0,80}?\d{1,3}(?:\.\d+)?\s*%[^.]{0,80}?"
        r"(?:lowest|average|vwap|volume[\s-]weighted)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"variable\s+conversion\s+price", re.IGNORECASE),
    re.compile(r"floating\s+conversion\s+price", re.IGNORECASE),
    re.compile(r"discount\s+to\s+(?:the\s+)?(?:then[\s-]current\s+)?market\s+price", re.IGNORECASE),
)


@dataclass(frozen=True)
class VariableConversionFinding:
    matched_pattern: str
    evidence: str


def detect_variable_conversion(text: str) -> VariableConversionFinding | None:
    """変動(デススパイラル型)転換価格条項を検知する。

    `conversion price ... N% of the ... lowest/average/VWAP ...` /
    `variable conversion price` / `floating conversion price` /
    `discount to the market price` のいずれかにマッチしたら、前後200字の
    原文を根拠として返す。固定転換価格(通常の転換社債)には反応しない。
    """
    for pattern in _VARIABLE_CONVERSION_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        start = max(0, match.start() - 200)
        end = min(len(text), match.end() + 200)
        return VariableConversionFinding(
            matched_pattern=pattern.pattern, evidence=text[start:end].strip()
        )
    return None


def options_ratio(unexercised_shares: float | None, shares_outstanding: float | None) -> float | None:
    """未行使オプション数 ÷ 発行済株式数。どちらかが欠けている・非正なら None。"""
    if unexercised_shares is None or shares_outstanding is None:
        return None
    if unexercised_shares < 0 or shares_outstanding <= 0:
        return None
    return unexercised_shares / shares_outstanding
