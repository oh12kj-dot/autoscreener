"""tests/unit/test_llm_batches.py(K-9)。

DBに触れるテストはローカル開発用Postgres(`docker compose up -d`)に対して
実行する。専用シンボル(ZZ***)を使い、終了時に削除する
(`tests/unit/test_collect_guidance.py` と同じ方針)。

**実APIは一切叩かない。** `LlmClient` の代役を差し込み、呼び出し回数まで
検査する——ここで押さえたい性質のひとつが「同じ入力で二度課金しないこと」
であり、それは戻り値ではなく呼び出し回数にしか現れないため。
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from autoscreener.batch.generate_report import generate_report
from autoscreener.batch.score_qualitative import score_qualitative
from autoscreener.batch.summarize_filings import summarize_filings
from autoscreener.config import LlmConfig
from autoscreener.db.models import FilingSection, LlmAnalysis, Score, Ticker
from autoscreener.db.session import session_scope
from autoscreener.llm.client import LlmResult, LlmUsage
from autoscreener.llm.qualitative import QualitativeAssessment

_SYMBOL = "ZZLLM9"
_TODAY = datetime.date(2026, 8, 30)
_CONFIG = LlmConfig(max_input_chars=10_000, max_tickers_per_run=5, max_output_tokens=2000)


# ---------------------------------------------------------------------------
# 代役
# ---------------------------------------------------------------------------


class _FakeTextClient:
    """`complete_text` だけを持つ代役。呼ばれた回数を数える。"""

    def __init__(self, text: str = "### 要旨\nテスト用の要約。") -> None:
        self.text = text
        self.calls: list[str] = []

    def complete_text(self, *, system, user, max_tokens=None) -> LlmResult:
        self.calls.append(user)
        return LlmResult(
            text=self.text,
            usage=LlmUsage(input_tokens=100, cache_read_tokens=800, output_tokens=50),
            model="claude-opus-5",
            request_id="req_fake",
        )


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 100
    cache_read_input_tokens = 800
    cache_creation_input_tokens = 0
    output_tokens = 50


class _BatchMessage:
    def __init__(self, payload: str) -> None:
        self.content = [_Block(payload)]
        self.stop_reason = "end_turn"
        self.stop_details = None
        self.usage = _Usage()
        self.model = "claude-opus-5"


class _BatchOutcome:
    def __init__(self, type_: str, message=None) -> None:
        self.type = type_
        self.message = message


class _BatchResult:
    def __init__(self, custom_id: str, outcome: _BatchOutcome) -> None:
        self.custom_id = custom_id
        self.result = outcome


_VALID_ASSESSMENT = (
    '{"business_summary":"半導体検査装置を売る。","moat_evidence":["switching costs"],'
    '"key_risks":["顧客集中"],"evidence_gaps":["価格戦略"],'
    '"conviction":"medium","conviction_rationale":"開示は具体的。"}'
)


class _FakeSequentialClient:
    """Batch 非対応プロバイダ(openai_compat)の代役。`parse_structured` だけを持つ。"""

    supports_batch = False
    provider_name = "openai_compat"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def parse_structured(self, *, system, user, output_model, max_tokens=None):
        self.calls.append(user)
        assessment = output_model.model_validate(
            {
                "business_summary": "半導体検査装置を売る。",
                "moat_evidence": ["switching costs"],
                "key_risks": ["顧客集中"],
                "evidence_gaps": ["価格戦略"],
                "conviction": "high",
                "conviction_rationale": "開示は具体的。",
            }
        )
        return assessment, LlmUsage(input_tokens=50, output_tokens=20), "req_seq"


class _FakeBatchClient:
    """Batch API の代役。submit で受けた custom_id をそのまま結果に返す。"""

    def __init__(self, payload: str = _VALID_ASSESSMENT, outcome_type: str = "succeeded") -> None:
        self.payload = payload
        self.outcome_type = outcome_type
        self.submitted: list = []
        self.waited: list[str] = []

    def submit_batch(self, requests) -> str:
        self.submitted.append(requests)
        return "msgbatch_fake"

    def wait_for_batch(self, batch_id: str):
        self.waited.append(batch_id)
        return object()

    def collect_batch(self, batch_id: str):
        for request in self.submitted[0]:
            yield _BatchResult(
                request["custom_id"],
                _BatchOutcome(self.outcome_type, _BatchMessage(self.payload)),
            )


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------


def _cleanup() -> None:
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one_or_none()
        if ticker is not None:
            session.query(LlmAnalysis).filter_by(ticker_id=ticker.id).delete()
            session.query(FilingSection).filter_by(ticker_id=ticker.id).delete()
            session.query(Score).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)
        # 銘柄横断の行(daily_report)は ticker_id が NULL なので上では消えない。
        session.query(LlmAnalysis).filter(
            LlmAnalysis.ticker_id.is_(None), LlmAnalysis.as_of == _TODAY
        ).delete()


@pytest.fixture
def ticker_id():
    _cleanup()
    with session_scope() as session:
        ticker = Ticker(symbol=_SYMBOL, market="US", sector="Technology")
        session.add(ticker)
        session.flush()
        tid = ticker.id
    yield tid
    _cleanup()


def _add_section(ticker_id: int, section: str, text: str, accession: str) -> None:
    with session_scope() as session:
        session.add(
            FilingSection(
                ticker_id=ticker_id,
                accession_number=accession,
                form="10-K",
                filed_date=datetime.date(2026, 3, 1),
                section=section,
                text=text,
                char_count=len(text),
                source_url="https://www.sec.gov/example",
                extracted_on=datetime.date(2026, 3, 2),
            )
        )


# ---------------------------------------------------------------------------
# summarize-filings
# ---------------------------------------------------------------------------


def test_summarize_filings_stores_content_refs_and_usage(ticker_id):
    _add_section(ticker_id, "item1a", "Risk factors body.", "0001-26-000001")
    client = _FakeTextClient()

    counts = summarize_filings(
        symbols=[_SYMBOL], sections=("item1a",), config=_CONFIG, client=client, today=_TODAY
    )

    assert counts["new_rows"] == 1
    assert len(client.calls) == 1
    with session_scope() as session:
        row = session.query(LlmAnalysis).filter_by(ticker_id=ticker_id).one()
        assert row.kind == "filing_summary"
        assert row.source_key == "0001-26-000001:item1a"
        assert row.content.startswith("### 要旨")
        assert row.data is None  # テキスト系なので構造化列は NULL
        assert row.source_refs[0]["accession_number"] == "0001-26-000001"
        # キャッシュが効いているかを後から検算できるように残す。
        assert row.usage["cache_read_tokens"] == 800


def test_summarize_filings_does_not_pay_twice_for_the_same_filing(ticker_id):
    """2度目は既存として飛ばす。**課金の有無は呼び出し回数にしか現れない。**"""
    _add_section(ticker_id, "item1a", "Risk factors body.", "0001-26-000001")
    client = _FakeTextClient()

    first = summarize_filings(
        symbols=[_SYMBOL], sections=("item1a",), config=_CONFIG, client=client, today=_TODAY
    )
    second = summarize_filings(
        symbols=[_SYMBOL], sections=("item1a",), config=_CONFIG, client=client, today=_TODAY
    )

    assert first["new_rows"] == 1
    assert second["new_rows"] == 0
    assert second["existing"] == 1
    assert len(client.calls) == 1, "2度目にAPIを呼んではならない(課金の無駄)"


def test_summarize_filings_changes_output_when_the_rubric_changes(ticker_id):
    """指示文(effort)が変われば指紋が変わり、新しい行として並ぶ。

    古い行を消さないのは、ルーブリック変更で何がどう変わったかを後から
    比較するため。
    """
    _add_section(ticker_id, "item1a", "Risk factors body.", "0001-26-000001")
    client = _FakeTextClient()

    summarize_filings(
        symbols=[_SYMBOL], sections=("item1a",), config=_CONFIG, client=client, today=_TODAY
    )
    summarize_filings(
        symbols=[_SYMBOL],
        sections=("item1a",),
        config=_CONFIG.model_copy(update={"effort": "low"}),
        client=client,
        today=_TODAY,
    )

    with session_scope() as session:
        rows = session.query(LlmAnalysis).filter_by(ticker_id=ticker_id).all()
    assert len(rows) == 2
    assert len({r.prompt_fingerprint for r in rows}) == 2


def test_oversized_section_is_counted_not_truncated(ticker_id):
    """上限超過は失敗として数え、**APIを呼ばない**。

    切り詰めて呼ぶと、読んでいない箇所について何も言わない要約が、完成品と
    区別できない形で残ってしまう。
    """
    _add_section(ticker_id, "item1a", "x" * 50_000, "0001-26-000001")
    client = _FakeTextClient()

    counts = summarize_filings(
        symbols=[_SYMBOL],
        sections=("item1a",),
        config=_CONFIG.model_copy(update={"max_input_chars": 100}),
        client=client,
        today=_TODAY,
    )

    assert counts["too_large"] == 1
    assert counts["new_rows"] == 0
    assert client.calls == []


# ---------------------------------------------------------------------------
# score-qualitative(Batch API)
# ---------------------------------------------------------------------------


def test_score_qualitative_stores_structured_advisory_output(ticker_id):
    _add_section(ticker_id, "item1a", "Risk factors body.", "0001-26-000001")
    _add_section(ticker_id, "item7", "MD&A body.", "0001-26-000001")
    client = _FakeBatchClient()

    counts = score_qualitative(
        symbols=[_SYMBOL], config=_CONFIG, client=client, today=_TODAY
    )

    assert counts["submitted"] == 1
    assert counts["new_rows"] == 1
    assert client.waited == ["msgbatch_fake"]
    with session_scope() as session:
        row = session.query(LlmAnalysis).filter_by(ticker_id=ticker_id, kind="qualitative").one()
        assert row.content is None  # 構造化系なのでテキスト列は NULL
        assert row.data["conviction"] == "medium"
        # 表を分けてあることに加えて、値そのものにも参考値の印を付ける。
        assert row.data["advisory"] is True
        assert row.data["not_used_in_gates_or_scores"] is True
        # 読んだ両セクションが記録されている。
        assert {r["section"] for r in row.source_refs} == {"item1a", "item7"}


def test_score_qualitative_request_declares_a_strict_schema(ticker_id):
    """バッチのリクエストに厳密スキーマが載っていること。

    載っていないと、モデルが自作の「総合点」を足して返しても素通りする。
    """
    _add_section(ticker_id, "item1a", "Risk factors body.", "0001-26-000001")
    client = _FakeBatchClient()

    score_qualitative(symbols=[_SYMBOL], sections=("item1a",), config=_CONFIG, client=client, today=_TODAY)

    params = client.submitted[0][0]["params"]
    fmt = params["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert params["thinking"] == {"type": "adaptive"}


def test_score_qualitative_skips_tickers_without_filing_text(ticker_id):
    client = _FakeBatchClient()
    counts = score_qualitative(symbols=[_SYMBOL], config=_CONFIG, client=client, today=_TODAY)
    assert counts["no_sections"] == 1
    assert counts["submitted"] == 0
    assert client.submitted == []


def test_score_qualitative_records_errored_results_without_storing_them(ticker_id):
    """失敗した結果を保存しない(数えるだけ)。"""
    _add_section(ticker_id, "item1a", "Risk factors body.", "0001-26-000001")
    client = _FakeBatchClient(outcome_type="expired")

    counts = score_qualitative(
        symbols=[_SYMBOL], sections=("item1a",), config=_CONFIG, client=client, today=_TODAY
    )

    assert counts["errored"] == 1
    assert counts["new_rows"] == 0
    with session_scope() as session:
        assert session.query(LlmAnalysis).filter_by(ticker_id=ticker_id).count() == 0


def test_score_qualitative_rejects_malformed_structured_output(ticker_id):
    """スキーマに合わないJSONは `parse_failures` として弾く。"""
    _add_section(ticker_id, "item1a", "Risk factors body.", "0001-26-000001")
    client = _FakeBatchClient(payload='{"business_summary":"x"}')

    counts = score_qualitative(
        symbols=[_SYMBOL], sections=("item1a",), config=_CONFIG, client=client, today=_TODAY
    )

    assert counts["parse_failures"] == 1
    assert counts["new_rows"] == 0


def test_score_qualitative_falls_back_to_sequential_without_batch_support(ticker_id):
    """`llm.provider = openai_compat` のように Batch 非対応なら、逐次の構造化
    出力で1銘柄ずつ評価・保存する(半額にはならないが結果は同じ形)。"""
    _add_section(ticker_id, "item1a", "Risk factors body.", "0001-26-000001")
    _add_section(ticker_id, "item7", "MD&A body.", "0001-26-000001")
    client = _FakeSequentialClient()

    counts = score_qualitative(symbols=[_SYMBOL], config=_CONFIG, client=client, today=_TODAY)

    assert counts["submitted"] == 1
    assert counts["new_rows"] == 1
    assert len(client.calls) == 1
    with session_scope() as session:
        row = session.query(LlmAnalysis).filter_by(ticker_id=ticker_id, kind="qualitative").one()
        assert row.data["conviction"] == "high"
        assert row.data["advisory"] is True
        assert row.content is None


def test_score_qualitative_sequential_does_not_pay_twice(ticker_id):
    _add_section(ticker_id, "item1a", "Risk factors body.", "0001-26-000001")
    client = _FakeSequentialClient()

    first = score_qualitative(
        symbols=[_SYMBOL], sections=("item1a",), config=_CONFIG, client=client, today=_TODAY
    )
    second = score_qualitative(
        symbols=[_SYMBOL], sections=("item1a",), config=_CONFIG, client=client, today=_TODAY
    )

    assert first["new_rows"] == 1
    assert second["existing"] == 1
    assert second["new_rows"] == 0
    assert len(client.calls) == 1, "2度目に呼んではならない(課金の無駄)"


# ---------------------------------------------------------------------------
# generate-report(銘柄横断=ticker_id が NULL)
# ---------------------------------------------------------------------------


def _add_score(ticker_id: int) -> None:
    with session_scope() as session:
        session.add(
            Score(
                ticker_id=ticker_id,
                score_date=_TODAY,
                scoring_version="v4",
                config_hash="deadbeef",
                probability=0.012,
                median_moic=3.5,
                factors={"revenue_multiple": 2.1},
                price_as_of=datetime.date(2026, 8, 20),
                financials_as_of=datetime.date(2026, 6, 30),
            )
        )


def test_generate_report_stores_a_ticker_less_row(ticker_id):
    _add_score(ticker_id)
    client = _FakeTextClient(text="### 今日の全体像\n候補は1件。")

    counts = generate_report(score_date=_TODAY, config=_CONFIG, client=client, today=_TODAY)

    assert counts["candidates"] == 1
    assert counts["new_rows"] == 1
    with session_scope() as session:
        row = (
            session.query(LlmAnalysis)
            .filter(LlmAnalysis.kind == "daily_report", LlmAnalysis.as_of == _TODAY)
            .one()
        )
        assert row.ticker_id is None
        assert row.source_key == _TODAY.isoformat()
        assert row.source_refs["ranked_symbols"][0]["symbol"] == _SYMBOL


def test_report_prompt_carries_the_precomputed_numbers(ticker_id):
    """レポートに渡すJSONに、算出済みの数字と鮮度が入っていること。

    モデルに再計算させないための前提。渡していない数字はレポートにも出せない。
    """
    _add_score(ticker_id)
    client = _FakeTextClient()
    generate_report(score_date=_TODAY, config=_CONFIG, client=client, today=_TODAY)

    sent = client.calls[0]
    assert '"probability": 0.012' in sent
    assert '"price_as_of": "2026-08-20"' in sent
    assert '"data_age_days"' in sent


def test_generate_report_does_not_pay_twice_for_the_same_day(ticker_id):
    _add_score(ticker_id)
    client = _FakeTextClient()

    generate_report(score_date=_TODAY, config=_CONFIG, client=client, today=_TODAY)
    second = generate_report(score_date=_TODAY, config=_CONFIG, client=client, today=_TODAY)

    assert second["existing"] == 1
    assert second["new_rows"] == 0
    assert len(client.calls) == 1


def test_partial_unique_index_blocks_duplicate_ticker_less_reports(ticker_id):
    """`ticker_id IS NULL` 側の部分ユニークインデックスが実際に効くこと。

    素の UNIQUE 制約では NULL 同士が「異なる」と扱われ、同じ日のレポートが
    何度でも入ってしまう。アプリ側の存在チェックだけに頼らず、DBでも塞いで
    あることをここで確かめる。
    """
    _add_score(ticker_id)
    client = _FakeTextClient()
    generate_report(score_date=_TODAY, config=_CONFIG, client=client, today=_TODAY)

    with session_scope() as session:
        existing = (
            session.query(LlmAnalysis)
            .filter(LlmAnalysis.kind == "daily_report", LlmAnalysis.as_of == _TODAY)
            .one()
        )
        duplicate = LlmAnalysis(
            ticker_id=None,
            kind=existing.kind,
            source_key=existing.source_key,
            as_of=existing.as_of,
            model=existing.model,
            effort=existing.effort,
            prompt_fingerprint=existing.prompt_fingerprint,
            content="重複",
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
