"""FINRA 空売り残高ファイルの実取得(K-7:docs/defect_and_edge_audit_2026-08-28.md I-5 の配線)。

`collectors/short_interest_source.py` にパース本体(`parse_short_interest` /
`ShortInterestRecord`)は既に実装済み。このモジュールはそこへ実データを
流し込む薄いHTTP取得層。

**実際に確認したURL・形式(2026-08-30、WebFetchで直接検証。推測ではない)**:

- ダウンロードページ: https://www.finra.org/finra-data/browse-catalog/equity-short-interest/files
  に掲載されているリンクを実際に踏んで確認した。
- URL: ``https://cdn.finra.org/equity/otcmarket/biweekly/shrt<YYYYMMDD>.csv``
  実在を確認した具体例:
    - https://cdn.finra.org/equity/otcmarket/biweekly/shrt20260731.csv
    - https://cdn.finra.org/equity/otcmarket/biweekly/shrt20260715.csv
- **拡張子は `.csv` だが実体はパイプ(`|`)区切り**(カンマ区切りではない)。
  実ファイルの先頭行を直接取得して確認した:
  ``accountingYearMonthNumber|symbolCode|issueName|
  issuerServicesGroupExchangeCode|marketClassCode|
  currentShortPositionQuantity|previousShortPositionQuantity|stockSplitFlag|
  averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag|changePercent|
  changePreviousNumber|settlementDate``
  これは `collectors/short_interest_source.py` が既に対応している列名
  (`symbolCode` / `currentShortPositionQuantity` /
  `previousShortPositionQuantity` / `averageDailyVolumeQuantity` /
  `daysToCoverQuantity` / `settlementDate`)とそのまま一致するため、
  変換なしで `parse_short_interest()` に渡せる。
- 実データの1行目に `issuerServicesGroupExchangeCode=NYSE` が含まれていた
  ことから、パス名は `otcmarket` のままだが**2021年6月以降はOTC限定では
  なく上場銘柄も含む consolidated 相当のファイル**になっている
  (FINRAのダウンロードページ本文の注記とも一致)。
- 更新頻度:半月ごと(biweekly)。決済日はおおむね月央(15日)と月末だが、
  週末・休場日にかかる場合は前営業日にずれる。正確な決済日カレンダーが
  公開されていないため、本モジュールは「狙いの日から数日遡って試し、
  404は握って次の候補へ」という方式で吸収する(下記 `_fetch_for_target`)。
- 認証:不要。公開CDNで、特別なヘッダも要らないことを確認した。

**設計上の要点(タスク仕様どおり)**:FINRAのファイルは**全銘柄が1ファイル**に
入っている。銘柄ごとにHTTPを叩く設計にすると数百リクエストになり無駄
——`fetch_short_interest_all()` で1回だけダウンロードし、シンボル別の辞書に
索いてから返す。`batch/collect_supply.py` 側はこの辞書を引くだけの
クロージャを既定fetcherとして使う。
"""

from __future__ import annotations

import calendar
import datetime
import logging

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from autoscreener.collectors.short_interest_source import ShortInterestRecord, parse_short_interest
from autoscreener.dates import utc_today

logger = logging.getLogger(__name__)

_BASE_URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{date}.csv"
_TIMEOUT_SECONDS = 30.0
# FINRA側は特別なUser-Agentを要求しない(WebFetchでの動作確認時に確認済み)が、
# 空UAでの機械的アクセスを避けるプロジェクトの流儀(edgar_client.py参照)に
# 合わせて名乗る。
_USER_AGENT = "AutoScreener/1.0 (research tool; FINRA short interest fetch)"

# 既定で遡る決済日の回数(半月ごと×6 ≒ 約3か月)。
_DEFAULT_PERIODS = 6
# 狙った決済日(15日/月末)がFINRA非公開日(週末・休場)にかかる場合に
# 遡って試す幅。FINRAの公開決済日カレンダーが無いための保守的な吸収策。
_DATE_SEARCH_WINDOW_DAYS = 5


class _FinraDownloadError(Exception):
    """このモジュール内だけで使うリトライ対象例外。404は含まない(呼び出し側で
    「ファイルが無い日」として扱うため、例外にはしない)。"""


def _month_end(year: int, month: int) -> datetime.date:
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _candidate_settlement_targets(reference: datetime.date, periods: int) -> list[datetime.date]:
    """`reference` 以前の「狙いの決済日」(15日・月末)を新しい順に `periods` 件。

    正確な決済日カレンダーは公開されていないため、ここでは典型的な
    アンカー(15日と月末)を列挙するだけ。実際の探索(週末・休場のズレ吸収)は
    `_fetch_for_target` が担当する。
    """
    months_needed = periods // 2 + 2
    year, month = reference.year, reference.month
    anchors: set[datetime.date] = set()
    for _ in range(months_needed):
        anchors.add(_month_end(year, month))
        anchors.add(datetime.date(year, month, 15))
        year, month = _prev_month(year, month)
    targets = sorted((d for d in anchors if d <= reference), reverse=True)
    return targets[:periods]


@retry(
    retry=retry_if_exception_type(_FinraDownloadError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1.0, max=10.0),
    reraise=True,
)
def _get_text(session: requests.Session, url: str) -> str | None:
    """200ならファイル本文、404なら None(存在しない決済日)。

    タイムアウト・接続エラー・404以外のHTTPエラーはリトライ対象
    (`_FinraDownloadError`)として上げる。
    """
    try:
        response = session.get(url, timeout=_TIMEOUT_SECONDS)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        raise _FinraDownloadError(str(exc)) from exc
    if response.status_code == 404:
        return None
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise _FinraDownloadError(str(exc)) from exc
    return response.text


def _fetch_for_target(session: requests.Session, target: datetime.date) -> str | None:
    """`target` から `_DATE_SEARCH_WINDOW_DAYS` 日遡りながら最初に見つかったファイルを返す。"""
    for offset in range(_DATE_SEARCH_WINDOW_DAYS + 1):
        candidate = target - datetime.timedelta(days=offset)
        url = _BASE_URL.format(date=candidate.strftime("%Y%m%d"))
        try:
            text = _get_text(session, url)
        except _FinraDownloadError:
            logger.warning("FINRA short interest: %s の取得に失敗", candidate, exc_info=True)
            continue
        if text is not None:
            return text
    return None


def fetch_short_interest_all(
    *,
    reference_date: datetime.date | None = None,
    periods: int = _DEFAULT_PERIODS,
    session: requests.Session | None = None,
) -> dict[str, list[ShortInterestRecord]]:
    """直近 `periods` 回分の決済日についてFINRAファイルをダウンロードし、
    シンボル別の辞書にまとめて返す。

    1回のバッチ実行につき最大 `periods` × (探索幅+1) 回のHTTPアクセスで済む
    ——ティッカー数に依存しない(K-7の設計要件。モジュールdocstring参照)。
    見つからない決済日は404として握って次の候補へ進む。
    """
    today = reference_date or utc_today()
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", _USER_AGENT)

    by_symbol: dict[str, list[ShortInterestRecord]] = {}
    for target in _candidate_settlement_targets(today, periods):
        text = _fetch_for_target(sess, target)
        if text is None:
            logger.info("FINRA short interest: %s 付近にファイルが見つからない", target)
            continue
        for record in parse_short_interest(text):
            by_symbol.setdefault(record.symbol, []).append(record)
    return by_symbol
