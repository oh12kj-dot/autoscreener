"""米国株ユニバースの調達(要件定義書 4章「要調達」の解決策)。

yfinance自体は銘柄一覧APIを持たず、S&P1500/Russell2000等の構成銘柄リストは
有償のため、NASDAQ Trader が無償・無認証で公開しているシンボルディレクトリ
(nasdaqlisted.txt / otherlisted.txt)を初期ユニバースの取得元とする。

このモジュールが返すのは「時価総額・売上規模で絞り込む前の広い候補リスト」。
ETF・テスト銘柄・普通株式以外(ADR/優先株/ワラント/ユニット等)はこの時点で
静的に除外できるが、時価総額・売上高ゲート(15.2・15.6)は実データ取得後の
次フェーズで適用する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# 普通株式以外を示す典型的な名称パターン(4章:ETF/REIT/ADRの除外、
# および一般的にスクリーニング対象にならない証券種別の除外)
_EXCLUDE_NAME_PATTERNS = re.compile(
    r"(depositary shares|american depositary|\bADR\b|\bADS\b"
    r"|real estate investment trust|\bREIT\b"
    r"|preferred|warrant|\bright(s)?\b|\bunit(s)?\b"
    r"|\bnotes?\b|\bbond(s)?\b|\bdebenture(s)?\b"
    r"|trust preferred|convertible)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CandidateTicker:
    symbol: str
    security_name: str
    exchange: str  # "NASDAQ" or "NYSE"
    is_etf: bool
    is_test_issue: bool


# NASDAQ Trader のシンボルディレクトリでは、優先株・ワラント・権利などの
# 「普通株式以外のクラス」がシンボル中の "$" で表される(FITB$I = Fifth Third の
# 優先株シリーズI)。
#
# **名称パターン(`_EXCLUDE_NAME_PATTERNS`)だけでは取りこぼす。** 実データで
# 25銘柄が名称に "preferred" 等を含まないまますり抜けており、日次収集の対象に
# なり続けていた(そのうえ、これらは普通株ではないので10バガー探索の対象として
# 意味がない)。シンボルの形状は名称より安定した判別材料である。
_NON_COMMON_SYMBOL_MARKERS = ("$",)


def _is_common_stock(security_name: str) -> bool:
    return _EXCLUDE_NAME_PATTERNS.search(security_name) is None


def _has_common_stock_symbol(symbol: str) -> bool:
    return not any(marker in symbol for marker in _NON_COMMON_SYMBOL_MARKERS)


def parse_nasdaq_listed(text: str) -> list[CandidateTicker]:
    """nasdaqlisted.txt をパースする。フッター行("File Creation Time: ...")は
    パイプ区切りのカラム数が合わないため自然に除外される。"""
    rows: list[CandidateTicker] = []
    lines = text.splitlines()
    header = lines[0].split("|")
    idx = {name: i for i, name in enumerate(header)}
    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"):
            continue  # フッター行(カラム数は本体行と一致するため列数だけでは判別できない)
        cols = line.split("|")
        if len(cols) != len(header):
            continue  # 不正行
        symbol = cols[idx["Symbol"]].strip()
        if not symbol:
            continue
        rows.append(
            CandidateTicker(
                symbol=symbol,
                security_name=cols[idx["Security Name"]].strip(),
                exchange="NASDAQ",
                is_etf=cols[idx["ETF"]].strip() == "Y",
                is_test_issue=cols[idx["Test Issue"]].strip() == "Y",
            )
        )
    return rows


def parse_other_listed(text: str) -> list[CandidateTicker]:
    """otherlisted.txt をパースする。Exchange == 'N'(NYSE)のみ採用し、
    NYSE Arca/AMEX/Cboe等は4章の対象市場外として除外する。"""
    rows: list[CandidateTicker] = []
    lines = text.splitlines()
    header = lines[0].split("|")
    idx = {name: i for i, name in enumerate(header)}
    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"):
            continue  # フッター行
        cols = line.split("|")
        if len(cols) != len(header):
            continue
        if cols[idx["Exchange"]].strip() != "N":
            continue
        symbol = cols[idx["ACT Symbol"]].strip()
        if not symbol:
            continue
        rows.append(
            CandidateTicker(
                symbol=symbol,
                security_name=cols[idx["Security Name"]].strip(),
                exchange="NYSE",
                is_etf=cols[idx["ETF"]].strip() == "Y",
                is_test_issue=cols[idx["Test Issue"]].strip() == "Y",
            )
        )
    return rows


def filter_candidates(candidates: list[CandidateTicker]) -> list[CandidateTicker]:
    """ETF・テスト銘柄・普通株式以外を除外する(4章)。"""
    return [
        c
        for c in candidates
        if not c.is_etf
        and not c.is_test_issue
        and _is_common_stock(c.security_name)
        # 名称で判別できない優先株等はシンボルの形状("$")で落とす
        and _has_common_stock_symbol(c.symbol)
        # yfinanceのティッカー表記はクラス株を "BRK-B" のようにハイフン区切りで
        # 扱うため、NASDAQ/NYSEの "BRK.B" 表記のドットを変換しておく
    ]


def fetch_universe_candidates(timeout_seconds: float = 30.0) -> list[CandidateTicker]:
    """NASDAQ Trader の公開シンボルディレクトリからユニバース候補を取得する。"""
    nasdaq_text = requests.get(NASDAQ_LISTED_URL, timeout=timeout_seconds).text
    other_text = requests.get(OTHER_LISTED_URL, timeout=timeout_seconds).text

    candidates = parse_nasdaq_listed(nasdaq_text) + parse_other_listed(other_text)
    filtered = filter_candidates(candidates)

    # yfinanceのティッカー表記に正規化(クラス株のドット→ハイフン)し重複排除
    seen: dict[str, CandidateTicker] = {}
    for c in filtered:
        normalized_symbol = c.symbol.replace(".", "-")
        if normalized_symbol not in seen:
            seen[normalized_symbol] = CandidateTicker(
                symbol=normalized_symbol,
                security_name=c.security_name,
                exchange=c.exchange,
                is_etf=c.is_etf,
                is_test_issue=c.is_test_issue,
            )
    return sorted(seen.values(), key=lambda c: c.symbol)
