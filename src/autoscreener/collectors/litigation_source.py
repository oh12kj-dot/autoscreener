"""訴訟・SEC調査・ショートレポートの検知(K-5)。

デューデリ11工程のうち「訴訟・ショートレポート」だけが Google 検索リンクしか
無く、完全に人間の手作業として残っていた工程の機械版。2つの経路を持つ:

1. **10-K Item 3(訴訟)の本文からの正規表現抽出**(`detect_litigation_mentions`、
   純関数・ネットワーク不要)。`filing_sections` に既に本文があるならこちらを
   優先する——SECへの再アクセスを増やさないため。
2. **EDGAR 全文検索APIへの問い合わせ**(`fetch_litigation`)。Item 3 に載らない
   8-K 開示(訴訟の新規発生・和解・ショートレポートへの反論)や、Item 3 が
   まだ `filing_sections` に無い銘柄を補う補助経路。

## 確認済みの実際のAPI仕様(2026-08-30、curlで実測。推測では書かない)

`collectors/edgar_client.py` は他担当が同時編集中のため触れられない。
全文検索は `data.sec.gov` / `www.sec.gov` とは別ドメイン(`efts.sec.gov`)の
別APIなので、独立した薄いクライアント `EdgarFullTextSearchClient` をここに置く
(レート制御は`collectors/rate_limit.py`の共有`sec`リミッター
——`EdgarClient`と同じインスタンス——をそのまま使う。2026-09-04是正:
以前は専用の`RateLimiter`インスタンスを持っていたが、SECインフラに対して
別々のレート制御を持つと合算レートが規約の想定を超えるため、共有インスタンス
へ揃えた。詳細は`EdgarFullTextSearchClient`のdocstring参照)。

- エンドポイント: ``GET https://efts.sec.gov/LATEST/search-index``
- 確認したクエリパラメータ:
  - ``q``      : 検索語。ダブルクオートで囲むとフレーズ完全一致になる
                 (例 ``q=%22securities+class+action%22``)。
  - ``forms``  : ルートフォームのカンマ区切り(例 ``forms=8-K,10-K``)。
  - ``ciks``   : 10桁ゼロ埋めCIKのカンマ区切り(例 ``ciks=0000320193``。
                 ダッシュ無し)。
  - ``dateRange=custom`` + ``startdt=YYYY-MM-DD`` + ``enddt=YYYY-MM-DD``。
  - `User-Agent` ヘッダは `data.sec.gov` 系と同様に連絡先メール必須(30.3.1と同じ制約)。
- 確認したレスポンス形(JSON、抜粋):

  .. code-block:: json

      {
        "hits": {
          "total": {"value": 2, "relation": "eq"},
          "hits": [
            {
              "_id": "0001085037-02-000265:ex101.htm",
              "_source": {
                "ciks": ["0001096550"],
                "display_names": ["FAIRCHILD INTERNATIONAL CORP  (CIK 0001096550)"],
                "root_forms": ["8-K"],
                "form": "8-K",
                "file_date": "2002-05-17",
                "adsh": "0001085037-02-000265",
                "file_type": "EX-10.1",
                "items": ["2"]
              }
            }
          ]
        }
      }

  ``adsh`` はダッシュ付きaccession number(`filings.accession_number` と同じ
  書式)。``_id`` は ``{accession(ダッシュ付き)}:{ファイル名}`` の形で、実ファイル
  URLの組み立てに使える。
- HTTP 200 かつ ``hits.total.value == 0`` が「該当なし」。403 は `data.sec.gov`
  と同様に ToS 違反(User-Agent不備・レート超過)を示す。
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass

import requests

from autoscreener.collectors.errors import (
    CollectionError,
    ParseFailure,
    PermanentFailure,
    TransientFailure,
)
from autoscreener.collectors.rate_limit import get_shared_limiter

logger = logging.getLogger(__name__)

FULL_TEXT_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

# 30.3.1 と同じ制約:プレースホルダーのUser-Agentで叩くとSECにIP遮断される。
_PLACEHOLDER_USER_AGENTS = frozenset({"", "your-address@example.com", "CHANGE_ME"})

_LOOKBACK_DAYS = 365 * 2
_FORMS = "8-K,10-K,10-Q"

# 種別ごとの検索フレーズ(全文検索APIへの問い合わせ用)。`batch/collect_litigation.py`
# が「全種別」を列挙するのに使うので公開名にする(`frozenset(LITIGATION_QUERY_PHRASES)`)。
LITIGATION_QUERY_PHRASES: dict[str, tuple[str, ...]] = {
    "class_action": ("securities class action", "putative class action"),
    "sec_investigation": ("subpoena from the SEC", "formal order of investigation", "Wells notice"),
    "short_report": ("short seller report", "short-seller"),
}


@dataclass(frozen=True)
class LitigationHit:
    """1件の検知結果(EDGAR全文検索経由、またはItem3本文経由どちらでも共通の形)。"""

    kind: str  # "class_action" / "sec_investigation" / "short_report"
    title: str
    event_date: datetime.date
    source_url: str | None
    source_accession: str | None
    detail: str | None


@dataclass(frozen=True)
class LitigationMention:
    """本文(主に10-K Item 3)の正規表現マッチ1件。

    日付・accession番号は `filing_sections` 行が持っているので、ここには
    含めない(呼び出し側がその行のメタデータと合わせて `LitigationHit` を組む)。
    """

    kind: str
    evidence: str


class EdgarFullTextSearchClient:
    """EDGAR全文検索(`efts.sec.gov`)専用の薄いクライアント。

    `EdgarClient`(`collectors/edgar_client.py`)は他担当が同時編集中で触れない
    ため、同じ設計(最小間隔方式のレート制御・403は`PermanentFailure`)を
    踏襲した独立クラスとしてここに置く。

    **2026-09-04是正(S-5監査、docs/daily_pipeline_throughput_plan_2026-09-04.md)**:
    以前はここで専用の`RateLimiter`インスタンスを作っていた
    (`edgar_client.RateLimiter`をimportして`RateLimiter(requests_per_second)`)。
    `collectors/rate_limit.py`のモジュールdocstringが名指しで警告している
    「プロセス内に複数のリミッターがあっても、SEC側から見れば1つの送信元」
    という anti-pattern そのものであり、SECは`efts.sec.gov`も含めsec.gov
    インフラ全体をIP単位で数える。litigationが逐次ループ(実測0.26 req/秒)
    だった間はこのリミッターが実質何も制限していなかったため表面化しな
    かったが、S-5で銘柄ループを並列化した結果、**このステージのレートを
    実際に決めているのはこのインスタンス1つだけ**になり、共有`sec`
    アカウンティングからは見えない状態になった。`get_shared_limiter("sec")`
    (`EdgarClient`と同じリミッター)へ揃える。
    """

    def __init__(self, user_agent: str, *, timeout_seconds: float = 10.0) -> None:
        if not user_agent or user_agent.strip() in _PLACEHOLDER_USER_AGENTS:
            raise ValueError(
                "EDGAR_USER_AGENT が未設定です。.env に EDGAR_USER_AGENT を設定してください"
                "(30.3.1と同じ制約:連絡先メールアドレスを含むUser-Agentが必須)。"
            )
        self._timeout = timeout_seconds
        # **プロセスで1つの共有リミッター**(`EdgarClient`と同じキー)。
        # `configure_shared_limiter`はここでは呼ばない——設定(`edgar.
        # requests_per_second`)を読む場所は`EdgarClient.__init__`だけに
        # 限定する(`rate_limit.py`のdocstring:「呼ぶのは設定を読んだ場所
        # だけにすること」)。未設定なら安全側の既定(5.0 req/秒)が使われる。
        self._rate_limiter = get_shared_limiter("sec")
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        )

    def search(
        self,
        q: str,
        *,
        forms: str | None = None,
        ciks: list[str] | None = None,
        start_date: datetime.date | None = None,
        end_date: datetime.date | None = None,
    ) -> dict:
        """全文検索APIを1回叩き、生JSONを返す(上記docstring参照)。"""
        params: dict[str, str] = {"q": q}
        if forms:
            params["forms"] = forms
        if ciks:
            params["ciks"] = ",".join(ciks)
        if start_date is not None and end_date is not None:
            params["dateRange"] = "custom"
            params["startdt"] = start_date.isoformat()
            params["enddt"] = end_date.isoformat()

        self._rate_limiter.acquire()
        try:
            response = self._session.get(FULL_TEXT_SEARCH_URL, params=params, timeout=self._timeout)
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 403:
                raise PermanentFailure(
                    f"EDGAR full text search returned 403 (likely a ToS violation): {exc}"
                ) from exc
            if status in (429, 500, 502, 503, 504):
                raise TransientFailure(str(exc)) from exc
            raise ParseFailure(f"unexpected HTTP status {status}: {exc}") from exc
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            raise TransientFailure(str(exc)) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise ParseFailure(f"full text search response is not valid JSON: {exc}") from exc


def parse_litigation_hits(payload: dict, kind: str) -> list[LitigationHit]:
    """全文検索APIの生JSONを `LitigationHit` のリストへ変換する(純関数)。"""
    results: list[LitigationHit] = []
    hits = ((payload.get("hits") or {}).get("hits")) or []
    for hit in hits:
        source = hit.get("_source") or {}
        adsh = source.get("adsh")
        file_date_raw = source.get("file_date")
        if not adsh or not file_date_raw:
            continue
        try:
            event_date = datetime.date.fromisoformat(file_date_raw)
        except ValueError:
            continue

        display_names = source.get("display_names") or []
        company = display_names[0] if display_names else ""
        form = source.get("form") or ""
        title = f"{form} {company}".strip() or kind

        source_url: str | None = None
        ciks = source.get("ciks") or []
        doc_id = hit.get("_id") or ""
        if ciks and ":" in doc_id:
            cik_str, filename = doc_id.split(":", 1)
            try:
                cik_int = int(ciks[0])
            except (TypeError, ValueError):
                cik_int = None
            if cik_int is not None:
                acc_nodash = adsh.replace("-", "")
                source_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{filename}"

        results.append(
            LitigationHit(
                kind=kind,
                title=title,
                event_date=event_date,
                source_url=source_url,
                source_accession=adsh,
                detail=None,
            )
        )
    return results


def fetch_litigation(
    client: EdgarFullTextSearchClient,
    ticker: str,
    cik: str,
    *,
    kinds: frozenset[str] | None = None,
    as_of: datetime.date | None = None,
) -> list[LitigationHit]:
    """1銘柄について、指定した種別(既定は全種別)を全文検索APIで問い合わせる。

    `client` を注入可能にしているのはテストでネットワークに出ないようにするため。
    種別ごとに複数のフレーズをOR的に(=個別クエリを積み上げて)投げる——全文検索
    APIの `q` は基本的にフレーズ一致であり、複数フレーズのORをAPI側だけで表現
    できないため。
    """
    today = as_of or datetime.date.today()
    start = today - datetime.timedelta(days=_LOOKBACK_DAYS)
    target_kinds = kinds if kinds is not None else frozenset(LITIGATION_QUERY_PHRASES)

    results: list[LitigationHit] = []
    seen: set[tuple[str, str]] = set()
    for kind in target_kinds:
        for phrase in LITIGATION_QUERY_PHRASES.get(kind, ()):
            try:
                payload = client.search(
                    f'"{phrase}"', forms=_FORMS, ciks=[cik], start_date=start, end_date=today
                )
            except CollectionError:
                logger.warning(
                    "%s: full text search failed for phrase %r", ticker, phrase, exc_info=True
                )
                continue
            for candidate in parse_litigation_hits(payload, kind):
                key = (candidate.kind, candidate.source_accession or "")
                if key in seen:
                    continue
                seen.add(key)
                results.append(candidate)
    return results


# --- Item 3(訴訟)本文の正規表現抽出 --------------------------------------------

# class_action:証券集団訴訟。「class action」単独(契約の仲裁条項にある
# クラスアクション放棄条項など)は無関係なので拾わない——
# 「securities class action」「putative class action」に限定する。
# sec_investigation: SEC調査の開始を示す定型句。
# short_report: ショートセラーレポートへの言及・反論開示。
_ITEM3_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "class_action": (
        re.compile(r"putative\s+(?:securities\s+)?class\s+action", re.IGNORECASE),
        re.compile(r"securities\s+class\s+action", re.IGNORECASE),
    ),
    "sec_investigation": (
        re.compile(r"subpoena\s+from\s+the\s+SEC", re.IGNORECASE),
        re.compile(r"formal\s+order\s+of\s+investigation", re.IGNORECASE),
        re.compile(r"Wells\s+notice", re.IGNORECASE),
    ),
    "short_report": (
        re.compile(r"short[- ]seller\s+report", re.IGNORECASE),
        re.compile(r"short[- ]seller", re.IGNORECASE),
    ),
}

_EVIDENCE_WINDOW = 200


def detect_litigation_mentions(text: str) -> list[LitigationMention]:
    """10-K Item 3 本文(または任意の提出書類本文)から訴訟関連の言及を拾う純関数。

    種別ごとに最初にマッチしたパターンのみを採用する(同じ段落内で複数の
    同義パターンが重複ヒットするのを避けるため)。
    """
    mentions: list[LitigationMention] = []
    for kind, patterns in _ITEM3_PATTERNS.items():
        for pattern in patterns:
            match = pattern.search(text)
            if match is None:
                continue
            start = max(0, match.start() - _EVIDENCE_WINDOW)
            end = min(len(text), match.end() + _EVIDENCE_WINDOW)
            mentions.append(LitigationMention(kind=kind, evidence=text[start:end].strip()))
            break
    return mentions
