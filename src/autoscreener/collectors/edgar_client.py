"""SEC EDGAR API の薄いラッパー(30.3)。

`yfinance_client.py` と同じ構造にしてある——リトライは tenacity、失敗は
`collectors/errors.py` の `CollectionError` 階層に分類、レート制御は
モジュール内で完結。**yfinanceと違い、SECは規約違反に対してIP単位の遮断で
応じる**ため、レート制御はベストエフォートではなく必須の要件として扱う。
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from autoscreener.collectors.errors import (
    CollectionError,
    EmptyResponseError,
    ParseFailure,
    PermanentFailure,
    TransientFailure,
)
from autoscreener.collectors.rate_limit import (  # noqa: F401 — RateLimiterは後方互換の再エクスポート
    RateLimiter,
    configure_shared_limiter,
    get_shared_limiter,
)
from autoscreener.config import EdgarConfig
from autoscreener.symbols import normalize_symbol

logger = logging.getLogger(__name__)

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
COMPANY_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
# B-1(docs/defect_and_edge_audit_2026-08-28.md I-1/I-2):四半期フルインデックス。
# `form.idx` は Form Type でソートされた固定幅テキスト。上場廃止届(Form 25/15)を
# 全期間走査して、期間中に消えた企業を復元するのに使う。
FULL_INDEX_FORM_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx"

# 30.3.1:8-Kの `items` は "2.02,9.01" のようなカンマ区切りのこともあれば
# "Item 2.02: Results of Operations..." のような説明つきのこともある。
# フォーマットの揺れをここで吸収し、下流には番号の配列だけを渡す。
_ITEM_NUMBER_PATTERN = re.compile(r"\b(\d\.\d{2})\b")

# HTTP 403 は規約違反(User-Agent不備・レート超過)を強く示唆するため、
# TransientFailureではなくPermanentFailureとして扱う(30.3.3)。
_PLACEHOLDER_USER_AGENTS = frozenset({"", "your-address@example.com", "CHANGE_ME"})


# 2026-08-30:`RateLimiter` は `collectors/rate_limit.py` に移した。
# **インスタンスごとに1つ持っていたのが誤り**だった——`EdgarClient(...)` を作る
# 箇所はコード上に9つあり、SECはIP単位で数えるので、実効レートは設定値の最大
# 9倍になりうる(詳しい経緯は移設先のモジュールdocstring)。
# 名前はテストと既存の import 互換のためにここからも見えるようにしておく。

def _parse_items(raw: str | None) -> list[str]:
    if not raw:
        return []
    return _ITEM_NUMBER_PATTERN.findall(raw)


@dataclass(frozen=True)
class FilingRecord:
    """`submissions` の1件分。DBの `filings` 行にそのまま対応する。"""

    accession_number: str  # "0001234567-25-000123"
    form: str  # "8-K" / "NT 10-Q" / "424B5" ...
    filed_date: datetime.date
    report_date: datetime.date | None
    items: list[str]  # 8-Kのアイテム番号(例 ["2.02", "9.01"])。他フォームは空
    primary_document: str | None
    document_url: str | None


def _document_url(cik_int: int, accession_no_nodash: str, primary_document: str | None) -> str | None:
    if not primary_document:
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_nodash}/{primary_document}"


def _classify_http_error(exc: requests.exceptions.HTTPError) -> CollectionError:
    status = exc.response.status_code if exc.response is not None else None
    if status == 403:
        # 30.3.3:規約違反(User-Agent不備・レート超過)を強く示唆する。
        # リトライで叩き続けると遮断が長引くため PermanentFailure として扱う。
        return PermanentFailure(f"EDGAR returned 403 (likely a ToS violation): {exc}")
    if status == 404:
        # CIKはあるが提出が無い/新規上場直後。
        return EmptyResponseError(f"EDGAR returned 404: {exc}")
    if status in (429, 500, 502, 503, 504):
        return TransientFailure(str(exc))
    return ParseFailure(f"unexpected HTTP status {status}: {exc}")


class EdgarClient:
    def __init__(self, config: EdgarConfig, user_agent: str) -> None:
        if not user_agent or user_agent.strip() in _PLACEHOLDER_USER_AGENTS:
            raise ValueError(
                "EDGAR_USER_AGENT が未設定です。.env に EDGAR_USER_AGENT を設定してください"
                "(例: EDGAR_USER_AGENT=\"TENX personal research <your-address@example.com>\")。"
                "SECは連絡先メールアドレスを含むUser-Agentを要求しており、これを守らないと"
                "IP単位で遮断されます(30.3.1)。"
            )
        self._config = config
        self._user_agent = user_agent
        # **プロセスで1つの共有リミッター**を設定値に合わせる。インスタンスごとに
        # 持たないのは、SECがIP単位で数えるため(rate_limit.py のdocstring)。
        self._rate_limiter = configure_shared_limiter("sec", config.requests_per_second)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        @retry(
            retry=retry_if_exception_type(TransientFailure),
            stop=stop_after_attempt(self._config.retry.max_attempts),
            wait=wait_exponential_jitter(
                initial=self._config.retry.backoff_base_seconds,
                max=self._config.retry.backoff_max_seconds,
            ),
            reraise=True,
        )
        def _call() -> requests.Response:
            # **リトライ側も必ずリミッターを通す。** 以前は `_get` の入口で1回
            # だけ acquire していたため、`max_attempts` 回のリトライは間隔制御を
            # 素通りしていた——しかもリトライが起きるのは相手が詰まっている
            # ときなので、最も投げてはいけない場面で最も速く投げていた。
            self._rate_limiter.acquire()
            try:
                response = self._session.get(url, timeout=self._config.timeout_seconds, **kwargs)
                response.raise_for_status()
                return response
            except requests.exceptions.HTTPError as exc:
                self._apply_server_backoff(exc.response)
                raise _classify_http_error(exc) from exc
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                raise TransientFailure(str(exc)) from exc

        return _call()

    def _apply_server_backoff(self, response: requests.Response | None) -> None:
        """サーバから「待て」と言われたら、**この用途の全リクエストを止める**。

        - 429 / 503 の `Retry-After` … 秒数が指定されていればそのとおり待つ。
          指定が無ければ `throttle_cooldown_seconds` を使う。tenacity の
          指数バックオフだけに任せると、サーバの指定より早く再送しうる。
        - 403 … SECの場合これは規約違反による遮断を強く示唆する(30.3.3)。
          個別のリクエストは `PermanentFailure` としてリトライしないが、
          **止めないと残りの銘柄ぶん叩き続けて遮断が長引く**。300銘柄あれば
          遮断中のSECに300本投げることになる。だから全体を冷ます。

        冷却は `throttle_cooldown_seconds` で上限を切ってある。実際の遮断は
        10分程度とされるが、無人のバッチをそこまで沈黙させるより、
        遅いレートで進めてログに残すほうが運用として扱いやすい。
        """
        if response is None:
            return
        status = response.status_code
        if status not in (403, 429, 503):
            return

        cooldown = self._config.throttle_cooldown_seconds
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                # 秒数形式のみ扱う。HTTP-date 形式は SEC では観測していないので、
                # 解釈を誤るより既定の冷却時間に倒す。
                cooldown = max(cooldown, float(retry_after.strip()))
            except ValueError:
                logger.warning("Retry-After を秒数として解釈できません: %r", retry_after)

        logger.warning(
            "EDGAR returned %s; pausing all SEC requests for %.0fs (url=%s)",
            status,
            cooldown,
            response.url,
        )
        self._rate_limiter.pause_for(cooldown)

    def fetch_company_tickers(self) -> dict[str, str]:
        """{正規化シンボル: 10桁CIK}。"""
        response = self._get(COMPANY_TICKERS_URL)
        try:
            data = response.json()
        except ValueError as exc:
            raise ParseFailure(f"company_tickers.json is not valid JSON: {exc}") from exc

        result: dict[str, str] = {}
        for entry in data.values():
            ticker = entry.get("ticker")
            cik = entry.get("cik_str")
            if not ticker or cik is None:
                continue
            result[normalize_symbol(ticker)] = f"{int(cik):010d}"
        return result

    def fetch_filings(self, cik: str, *, forms: set[str] | None = None) -> list[FilingRecord]:
        """`filings.recent` を FilingRecord に整形する。forms 指定で絞り込む。

        `filings.recent` は列指向の並列配列(accessionNumber / filingDate /
        reportDate / form / items / primaryDocument が同じ長さの配列で並ぶ)。
        古い分は `filings.files[]` に別JSONへのポインタが入るが、本計画では
        `recent` だけを使う(30.3.1:直近1000件・約1年分あれば十分)。
        """
        response = self._get(SUBMISSIONS_URL.format(cik=cik))
        try:
            data = response.json()
        except ValueError as exc:
            raise ParseFailure(f"submissions JSON is not valid: {exc}") from exc

        recent = (data.get("filings") or {}).get("recent") or {}
        accession_numbers = recent.get("accessionNumber") or []
        forms_list = recent.get("form") or []
        filing_dates = recent.get("filingDate") or []
        report_dates = recent.get("reportDate") or []
        items_list = recent.get("items") or []
        primary_documents = recent.get("primaryDocument") or []

        n = len(accession_numbers)
        if not (len(forms_list) == n and len(filing_dates) == n):
            raise ParseFailure(
                f"filings.recent の並列配列の長さが揃っていません "
                f"(accessionNumber={n}, form={len(forms_list)}, filingDate={len(filing_dates)})"
            )

        cik_int = int(cik)
        records: list[FilingRecord] = []
        for i in range(n):
            form = forms_list[i]
            if forms is not None and form not in forms:
                continue
            accession = accession_numbers[i]
            accession_nodash = accession.replace("-", "")
            primary_document = primary_documents[i] if i < len(primary_documents) else None
            report_date_raw = report_dates[i] if i < len(report_dates) else None
            items_raw = items_list[i] if i < len(items_list) else None
            records.append(
                FilingRecord(
                    accession_number=accession,
                    form=form,
                    filed_date=datetime.date.fromisoformat(filing_dates[i]),
                    report_date=datetime.date.fromisoformat(report_date_raw) if report_date_raw else None,
                    items=_parse_items(items_raw),
                    primary_document=primary_document,
                    document_url=_document_url(cik_int, accession_nodash, primary_document),
                )
            )
        return records

    def fetch_full_index_form(self, year: int, quarter: int) -> str:
        """`full-index/{year}/QTR{q}/form.idx` の生テキスト(B-1)。"""
        response = self._get(FULL_INDEX_FORM_URL.format(year=year, quarter=quarter))
        return response.text

    def fetch_company_facts(self, cik: str) -> dict:
        """companyfacts の生JSON。フェーズ4で使う。"""
        response = self._get(COMPANY_FACTS_URL.format(cik=cik))
        try:
            return response.json()
        except ValueError as exc:
            raise ParseFailure(f"companyfacts JSON is not valid: {exc}") from exc

    def fetch_company_concept(self, cik: str, taxonomy: str, tag: str) -> dict:
        """companyconcept(単一タグ)の生JSON。"""
        response = self._get(COMPANY_CONCEPT_URL.format(cik=cik, taxonomy=taxonomy, tag=tag))
        try:
            return response.json()
        except ValueError as exc:
            raise ParseFailure(f"companyconcept JSON is not valid: {exc}") from exc

    def fetch_document_text(self, url: str, *, max_bytes: int = 8_000_000) -> tuple[str, bool]:
        """提出書類本文をプレーンテキストにして返す(lxmlでタグを除去)。

        `max_bytes` を設けるのは、10-Kが数十MBになる銘柄が実在し、
        全銘柄ぶんをメモリに載せると日次バッチが落ちるため。
        超過分は切り捨てる——going concern の記載は監査報告書と
        流動性の節にあり、文書前半〜中盤に現れるので実務上支障がない。

        戻り値は (本文テキスト, 切り捨てたか) のタプル。
        """
        from lxml import html as lxml_html

        self._rate_limiter.acquire()
        try:
            response = self._session.get(url, timeout=self._config.timeout_seconds, stream=True)
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise _classify_http_error(exc) from exc
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            raise TransientFailure(str(exc)) from exc

        chunks: list[bytes] = []
        total = 0
        truncated = False
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            remaining = max_bytes - total
            if remaining <= 0:
                truncated = True
                break
            chunks.append(chunk[:remaining])
            total += len(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
                break

        raw = b"".join(chunks)
        try:
            tree = lxml_html.fromstring(raw)
            text = tree.text_content()
        except Exception:
            # 断片HTML(切り捨てにより閉じタグが欠けている等)はパース失敗しうる。
            # その場合は生バイトをそのままデコードして正規表現検索に回す——
            # 完全なテキスト抽出でなくても going concern の検出には十分。
            text = raw.decode("utf-8", errors="ignore")
        return text, truncated

    # --- K-1(自動化計画 2026-08-30)------------------------------------------
    #
    # 以下2つは「提出書類の**中身**を機械で読む」ための汎用経路。既存の
    # `fetch_document_text` は主文書1つをHTML除去して返すが、
    #   * 8-K の EX-99.1(決算プレスリリース)は主文書ではなく添付である
    #   * Form 4 は XML であり、lxml.html でタグを剥がすと構造が壊れる
    # という2点でそれでは足りない。**この2メソッドは共有基盤なので、
    # K-2〜K-6 の各実装はここを書き換えず呼ぶだけにすること**(同一ファイルを
    # 複数の実装が同時に触ると壊れる)。

    def fetch_filing_index(self, cik: str, accession_number: str) -> list[dict]:
        """1件の提出書類に含まれるファイル一覧を返す。

        戻り値は `index.json` の `directory.item` そのまま(`name` / `type` /
        `size` 等)。呼び出し側は `name` から目的の添付(EX-99.1 の htm 等)を
        選び、`filing_file_url()` で URL を組み立てる。
        """
        cik_int = int(cik)
        acc = accession_number.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/index.json"
        response = self._get(url)
        payload = response.json()
        items = payload.get("directory", {}).get("item", [])
        return [item for item in items if isinstance(item, dict)]

    def fetch_raw(self, url: str, *, max_bytes: int = 8_000_000) -> str:
        """URL の中身をデコードだけして返す(HTMLタグを除去しない)。

        XML(Form 4)のように構造そのものが情報である文書に使う。
        `fetch_document_text` と同じく上限バイト数で打ち切る。
        """
        response = self._get(url, stream=True)
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            remaining = max_bytes - total
            if remaining <= 0:
                break
            chunks.append(chunk[:remaining])
            total += len(chunk[:remaining])
            if len(chunk) > remaining:
                break
        return b"".join(chunks).decode("utf-8", errors="ignore")


def filing_file_url(cik: str, accession_number: str, filename: str) -> str:
    """`fetch_filing_index` が返した `name` から実ファイルURLを組み立てる(K-1)。"""
    cik_int = int(cik)
    acc = accession_number.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{filename}"
