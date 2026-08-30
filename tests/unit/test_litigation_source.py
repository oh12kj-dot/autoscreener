"""tests/unit/test_litigation_source.py(K-5)。

`collectors/litigation_source.py` のうち `detect_litigation_mentions` と
`parse_litigation_hits` は純関数(ネットワーク・DB不要)。ネットワークに
出るのは `EdgarFullTextSearchClient.search` / `fetch_litigation` のみで、
ここでは実際の通信は行わずJSONペイロードを直接組み立ててテストする。
誤検出しないことのテスト(紛らわしいが該当しない文)を必ず含める。
"""

from __future__ import annotations

import datetime

from autoscreener.collectors.litigation_source import (
    LitigationHit,
    detect_litigation_mentions,
    fetch_litigation,
    parse_litigation_hits,
)

# --- detect_litigation_mentions(Item 3 本文の正規表現抽出) ---------------------

_CLASS_ACTION_TEXT = """
Item 3. Legal Proceedings

On March 12, 2026, a putative securities class action was filed in the
United States District Court for the Southern District of New York against
the Company and certain of its officers, alleging violations of the federal
securities laws.
"""

_SEC_INVESTIGATION_TEXT = """
Item 3. Legal Proceedings

In June 2026, the Company received a subpoena from the SEC requesting
documents related to its revenue recognition practices for fiscal 2025.
"""

_SHORT_REPORT_TEXT = """
Item 3. Legal Proceedings

On July 8, 2026, a short-seller report was published alleging accounting
irregularities at the Company. The Company believes the allegations in the
report are without merit.
"""

_ARBITRATION_CLASS_ACTION_WAIVER_TEXT = """
Item 3. Legal Proceedings

The Company is not currently a party to any material legal proceedings.
Our standard customer agreements include an arbitration clause with a class
action waiver, consistent with industry practice.
"""

_INTERNAL_INVESTIGATION_TEXT = """
Item 3. Legal Proceedings

The Audit Committee engaged outside counsel to conduct an internal
investigation into certain expense reimbursement practices. The internal
investigation concluded in May 2026 with no findings of material misconduct.
"""

_SHORT_TERM_INVESTMENTS_TEXT = """
Item 3. Legal Proceedings

There are no material pending legal proceedings to which the Company is a
party. See Note 4 for a discussion of the Company's short-term investments.
"""


def test_detect_litigation_mentions_class_action():
    mentions = detect_litigation_mentions(_CLASS_ACTION_TEXT)
    kinds = {m.kind for m in mentions}
    assert "class_action" in kinds
    hit = next(m for m in mentions if m.kind == "class_action")
    assert "putative securities class action" in hit.evidence


def test_detect_litigation_mentions_sec_investigation():
    mentions = detect_litigation_mentions(_SEC_INVESTIGATION_TEXT)
    kinds = {m.kind for m in mentions}
    assert "sec_investigation" in kinds
    hit = next(m for m in mentions if m.kind == "sec_investigation")
    assert "subpoena from the SEC" in hit.evidence


def test_detect_litigation_mentions_short_report():
    mentions = detect_litigation_mentions(_SHORT_REPORT_TEXT)
    kinds = {m.kind for m in mentions}
    assert "short_report" in kinds


def test_detect_litigation_mentions_false_positive_arbitration_waiver():
    # 契約上の「クラスアクション放棄条項」への言及は、証券集団訴訟ではない。
    mentions = detect_litigation_mentions(_ARBITRATION_CLASS_ACTION_WAIVER_TEXT)
    assert "class_action" not in {m.kind for m in mentions}


def test_detect_litigation_mentions_false_positive_internal_investigation():
    # 「internal investigation」はSECの正式調査(formal order of investigation /
    # subpoena from the SEC)ではないので拾わない。
    mentions = detect_litigation_mentions(_INTERNAL_INVESTIGATION_TEXT)
    assert "sec_investigation" not in {m.kind for m in mentions}


def test_detect_litigation_mentions_false_positive_short_term_investments():
    # 「short-term investments」は「short-seller」と無関係。
    mentions = detect_litigation_mentions(_SHORT_TERM_INVESTMENTS_TEXT)
    assert "short_report" not in {m.kind for m in mentions}


def test_detect_litigation_mentions_no_hits_on_clean_filing():
    text = "Item 3. Legal Proceedings\n\nThe Company is not currently a party to any material legal proceedings."
    assert detect_litigation_mentions(text) == []


# --- parse_litigation_hits(EDGAR全文検索APIレスポンスの解析) -------------------

_SAMPLE_SEARCH_PAYLOAD = {
    "hits": {
        "total": {"value": 1, "relation": "eq"},
        "hits": [
            {
                "_id": "0001564590-26-011839:ex991.htm",
                "_source": {
                    "ciks": ["0001077183"],
                    "display_names": ["EXAMPLE CORP  (EX)  (CIK 0001077183)"],
                    "root_forms": ["8-K"],
                    "form": "8-K",
                    "file_date": "2026-05-30",
                    "adsh": "0001564590-26-011839",
                    "file_type": "EX-99.1",
                    "items": ["8.01"],
                },
            }
        ],
    }
}


def test_parse_litigation_hits_basic():
    hits = parse_litigation_hits(_SAMPLE_SEARCH_PAYLOAD, "class_action")
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, LitigationHit)
    assert hit.kind == "class_action"
    assert hit.event_date == datetime.date(2026, 5, 30)
    assert hit.source_accession == "0001564590-26-011839"
    assert hit.source_url == (
        "https://www.sec.gov/Archives/edgar/data/1077183/000156459026011839/ex991.htm"
    )
    assert "EXAMPLE CORP" in hit.title


def test_parse_litigation_hits_empty_when_no_hits():
    payload = {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
    assert parse_litigation_hits(payload, "class_action") == []


def test_parse_litigation_hits_skips_entries_without_accession_or_date():
    payload = {
        "hits": {
            "hits": [
                {"_id": "x:y.htm", "_source": {"ciks": ["1"], "form": "8-K"}},  # adshが無い
                {
                    "_id": "0001-26-000001:y.htm",
                    "_source": {"ciks": ["1"], "form": "8-K", "adsh": "0001-26-000001"},
                },  # file_dateが無い
            ]
        }
    }
    assert parse_litigation_hits(payload, "class_action") == []


# --- fetch_litigation(clientを注入してネットワークに出ないことを確認) -----------


class _FakeSearchClient:
    """テスト用のフェイク検索クライアント。呼び出しごとの引数を記録する。"""

    def __init__(self, payload_by_query: dict[str, dict]) -> None:
        self._payload_by_query = payload_by_query
        self.calls: list[dict] = []

    def search(self, q, *, forms=None, ciks=None, start_date=None, end_date=None):
        self.calls.append({"q": q, "forms": forms, "ciks": ciks})
        return self._payload_by_query.get(q, {"hits": {"hits": []}})


def test_fetch_litigation_only_queries_requested_kinds():
    client = _FakeSearchClient({})
    fetch_litigation(client, "ZZLIT1", "0000320193", kinds=frozenset({"short_report"}))
    queried_phrases = {call["q"] for call in client.calls}
    assert queried_phrases == {'"short seller report"', '"short-seller"'}
    for call in client.calls:
        assert call["ciks"] == ["0000320193"]


def test_fetch_litigation_aggregates_and_dedupes_hits():
    payload = {
        "hits": {
            "hits": [
                {
                    "_id": "0001-26-000001:ex991.htm",
                    "_source": {
                        "ciks": ["0000320193"],
                        "display_names": ["ZZLIT1 CORP"],
                        "form": "8-K",
                        "file_date": "2026-06-01",
                        "adsh": "0001-26-000001",
                    },
                }
            ]
        }
    }
    client = _FakeSearchClient(
        {'"securities class action"': payload, '"putative class action"': payload}
    )
    hits = fetch_litigation(client, "ZZLIT1", "0000320193", kinds=frozenset({"class_action"}))
    # 同じaccessionが2つのフレーズ双方にヒットしても1件にまとめる。
    assert len(hits) == 1
    assert hits[0].source_accession == "0001-26-000001"
