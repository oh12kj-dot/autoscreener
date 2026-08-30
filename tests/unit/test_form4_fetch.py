"""K-7:`collectors/form4_fetch.py` のテスト。ネットワークに出ない
——`EdgarClient` を模したフェイクを注入する。"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.collectors.errors import PermanentFailure
from autoscreener.collectors.form4_fetch import (
    _owner_name,
    _role_label,
    _select_xml_document,
    fetch_form4_rows,
)
from autoscreener.collectors.form4_source import Form4Transaction

# 実際のForm4提出に近いXML断片。reportingOwnerId/rptOwnerName を含める
# (parse_form4 自体は見ないが、form4_fetch 側が個別に読む対象)。
_FORM4_XML_RECENT = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001111111</rptOwnerCik>
      <rptOwnerName>DOE JANE</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-08-10</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>3.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# cutoff より古い(除外されるべき)取引だけを含む提出。
_FORM4_XML_OLD = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0002222222</rptOwnerCik>
      <rptOwnerName>SMITH JOHN</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>0</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2020-01-05</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>2.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


class _FakeFilingRecord:
    """`EdgarClient.FilingRecord` の代わりに使う最小限のフェイク。"""

    def __init__(self, accession_number: str, filed_date: datetime.date) -> None:
        self.accession_number = accession_number
        self.filed_date = filed_date
        self.form = "4"


class _FakeEdgarClient:
    """ネットワークに出ないフェイク。テストごとにfixtureで組み立てる。"""

    def __init__(self, filings, index_by_accession, xml_by_url, *, raise_on_filings=None):
        self._filings = filings
        self._index_by_accession = index_by_accession
        self._xml_by_url = xml_by_url
        self._raise_on_filings = raise_on_filings
        self.raw_calls: list[str] = []

    def fetch_filings(self, cik: str, *, forms=None):
        if self._raise_on_filings is not None:
            raise self._raise_on_filings
        return self._filings

    def fetch_filing_index(self, cik: str, accession_number: str):
        return self._index_by_accession[accession_number]

    def fetch_raw(self, url: str) -> str:
        self.raw_calls.append(url)
        return self._xml_by_url[url]


def _index_url(cik: str, accession: str, filename: str) -> str:
    # collectors.edgar_client.filing_file_url と同じ組み立て規則。
    from autoscreener.collectors.edgar_client import filing_file_url

    return filing_file_url(cik, accession, filename)


def test_select_xml_document_excludes_xsl_rendering():
    """EDGARは同じ提出に xslF345X03/....xml(HTMLレンダリング)と生 .xml の
    両方を置く。xsl を含む名前は除外し、生XMLだけを選ぶこと。"""
    items = [
        {"name": "xslF345X03/wf-form4_1234567890.xml", "type": "text.xml"},
        {"name": "wf-form4_1234567890.xml", "type": "text.xml"},
        {"name": "primary_doc.xml", "type": "text.xml"},
        {"name": "0001234567-26-000123-index.htm", "type": "text.html"},
    ]
    selected = _select_xml_document(items)
    assert selected is not None
    assert "xsl" not in selected["name"].lower()
    # 複数の非XSL候補が残る場合はファイル名でソートして決定的に選ぶ。
    assert selected["name"] == "primary_doc.xml"


def test_select_xml_document_returns_none_when_only_xsl_present():
    items = [{"name": "xslF345X03/wf-form4_1234567890.xml", "type": "text.xml"}]
    assert _select_xml_document(items) is None


def test_owner_name_extracts_rpt_owner_name():
    assert _owner_name(_FORM4_XML_RECENT) == "DOE JANE"


def test_owner_name_returns_none_for_malformed_xml():
    assert _owner_name("<not-xml") is None


def test_role_label_prioritizes_officer_title():
    txn = Form4Transaction(
        transaction_date=datetime.date(2026, 8, 10),
        code="P",
        shares=5000,
        price_per_share=3.5,
        acquired=True,
        is_director=True,
        is_officer=True,
        is_ten_percent_owner=False,
        officer_title="CEO",
        shares_owned_following=None,
    )
    assert _role_label(txn) == "CEO"


def test_role_label_falls_back_to_director():
    txn = Form4Transaction(
        transaction_date=datetime.date(2026, 8, 10),
        code="P",
        shares=5000,
        price_per_share=3.5,
        acquired=True,
        is_director=True,
        is_officer=False,
        is_ten_percent_owner=False,
        officer_title=None,
        shares_owned_following=None,
    )
    assert _role_label(txn) == "Director"


def test_fetch_form4_rows_builds_insider_rows_and_filters_by_date():
    cik = "0000123456"
    accession_recent = "0000123456-26-000111"
    accession_old = "0000123456-20-000001"

    filings = [
        _FakeFilingRecord(accession_recent, datetime.date(2026, 8, 12)),
        _FakeFilingRecord(accession_old, datetime.date(2020, 1, 7)),
    ]
    index_by_accession = {
        accession_recent: [
            {"name": "xslF345X03/wf-form4_1.xml", "type": "text.xml"},
            {"name": "wf-form4_1.xml", "type": "text.xml"},
        ],
        accession_old: [
            {"name": "wf-form4_2.xml", "type": "text.xml"},
        ],
    }
    url_recent = _index_url(cik, accession_recent, "wf-form4_1.xml")
    url_old = _index_url(cik, accession_old, "wf-form4_2.xml")
    xml_by_url = {url_recent: _FORM4_XML_RECENT, url_old: _FORM4_XML_OLD}

    client = _FakeEdgarClient(filings, index_by_accession, xml_by_url)

    # cutoffを2025-01-01にすると、filed_dateによる事前フィルタで
    # accession_old(2020年提出)自体がネットワーク往復の対象から外れる。
    rows = fetch_form4_rows(client, cik, since=datetime.date(2025, 1, 1))

    assert len(rows) == 1
    row = rows[0]
    assert row.accession_number == accession_recent
    assert row.insider_name == "DOE JANE"
    assert row.transaction_code == "P"
    assert row.shares == 5000
    assert row.role == "CEO"
    assert row.price_usd == 3.5
    assert row.value_usd == 5000 * 3.5
    assert row.is_derivative is False
    # xsl版のURLは一度もfetch_rawされていないこと(除外の検証)。
    assert all("xsl" not in u.lower() for u in client.raw_calls)


def test_fetch_form4_rows_max_filings_caps_processed_count():
    cik = "0000999999"
    filings = [
        _FakeFilingRecord(f"0000999999-26-{i:06d}", datetime.date(2026, 8, i))
        for i in range(1, 6)
    ]
    index_by_accession = {}
    xml_by_url = {}
    for f in filings:
        fname = f"wf-form4_{f.accession_number}.xml"
        index_by_accession[f.accession_number] = [{"name": fname, "type": "text.xml"}]
        url = _index_url(cik, f.accession_number, fname)
        xml_by_url[url] = _FORM4_XML_RECENT

    client = _FakeEdgarClient(filings, index_by_accession, xml_by_url)
    rows = fetch_form4_rows(client, cik, since=datetime.date(2020, 1, 1), max_filings=2)
    # 新しい順に2件だけ処理されるので、行数も2件(各提出1取引)まで。
    assert len(rows) == 2


def test_fetch_form4_rows_returns_empty_on_filings_fetch_failure():
    client = _FakeEdgarClient([], {}, {}, raise_on_filings=PermanentFailure("boom"))
    assert fetch_form4_rows(client, "0000000001") == []
