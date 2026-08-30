"""`GET /llm/report` / `GET /llm/{ticker}` / `GET /llm/providers` /
`POST /llm/report/generate` のテスト(K-9)。

`docker compose up -d` のローカル開発用Postgresに対して実行する。専用シンボル
(ZZ***)でデータを作り、終了時に削除する(`test_api_routes.py` と同じ方針)。

**読み取りは無条件。** `POST /llm/report/generate` だけが書き込みで、課金が
伴うので `confirm=true` 必須・30秒レート制限・同時実行ロックを検査する
(ui_llm_provider_selection_2026-08-30.md)。実LLMは叩かず `generate_report` を
差し替える。
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

import autoscreener.api.routes as routes
import autoscreener.batch.generate_report as gen_mod
from autoscreener.api.main import app
from autoscreener.api.schemas import LLM_DISCLAIMER
from autoscreener.db.models import LlmAnalysis, Ticker
from autoscreener.db.session import session_scope
from autoscreener.llm.errors import LlmDisabled

client = TestClient(app)

_SYMBOL = "ZZAPILLM"
_AS_OF = datetime.date(2099, 1, 2)  # 実データと衝突しない未来日付
_SCORE_DATE = datetime.date(2099, 1, 1)


def _cleanup() -> None:
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one_or_none()
        if ticker is not None:
            session.query(LlmAnalysis).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)
        session.query(LlmAnalysis).filter(
            LlmAnalysis.ticker_id.is_(None), LlmAnalysis.as_of == _AS_OF
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


def _add_summary(ticker_id: int) -> None:
    with session_scope() as session:
        session.add(
            LlmAnalysis(
                ticker_id=ticker_id,
                kind="filing_summary",
                source_key="0001-99-000001:item1a",
                as_of=_AS_OF,
                model="claude-opus-5",
                effort="high",
                prompt_fingerprint="f" * 64,
                content="### 要旨\n本文の要約。",
                source_refs=[
                    {
                        "accession_number": "0001-99-000001",
                        "form": "10-K",
                        "section": "item1a",
                        "filed_date": "2099-01-01",
                        "source_url": "https://www.sec.gov/example",
                    }
                ],
                usage={
                    "input_tokens": 100,
                    "cache_read_tokens": 800,
                    "cache_creation_tokens": 0,
                    "output_tokens": 50,
                },
            )
        )


def _add_qualitative(ticker_id: int, *, as_of: datetime.date, conviction: str) -> None:
    with session_scope() as session:
        session.add(
            LlmAnalysis(
                ticker_id=ticker_id,
                kind="qualitative",
                source_key=f"key-{conviction}",
                as_of=as_of,
                model="claude-opus-5",
                effort="high",
                prompt_fingerprint="a" * 64,
                data={
                    "advisory": True,
                    "not_used_in_gates_or_scores": True,
                    "business_summary": "半導体検査装置を売る。",
                    "moat_evidence": ["switching costs"],
                    "key_risks": ["顧客集中"],
                    "evidence_gaps": ["価格戦略"],
                    "conviction": conviction,
                    "conviction_rationale": "開示は具体的。",
                },
                source_refs=[{"accession_number": "0001-99-000001", "section": "item1a"}],
            )
        )


def _add_report() -> None:
    with session_scope() as session:
        session.add(
            LlmAnalysis(
                ticker_id=None,
                kind="daily_report",
                source_key=_SCORE_DATE.isoformat(),
                as_of=_AS_OF,
                model="claude-opus-5",
                effort="high",
                prompt_fingerprint="b" * 64,
                content="### 今日の全体像\n候補は1件。",
                source_refs={
                    "as_of": _SCORE_DATE.isoformat(),
                    "ranked_symbols": [{"rank": 1, "symbol": _SYMBOL}],
                },
            )
        )


# ---------------------------------------------------------------------------
# GET /llm/{ticker}
# ---------------------------------------------------------------------------


def test_ticker_analysis_returns_summaries_and_qualitative(ticker_id):
    _add_summary(ticker_id)
    _add_qualitative(ticker_id, as_of=_AS_OF, conviction="medium")

    body = client.get(f"/api/v1/llm/{_SYMBOL}").json()

    assert body["ticker"] == _SYMBOL
    assert len(body["summaries"]) == 1
    assert body["summaries"][0]["content"].startswith("### 要旨")
    assert body["summaries"][0]["source_refs"][0]["accession_number"] == "0001-99-000001"
    assert body["summaries"][0]["usage"]["cache_read_tokens"] == 800
    assert body["qualitative"]["conviction"] == "medium"
    assert body["qualitative"]["key_risks"] == ["顧客集中"]


def test_every_response_carries_the_advisory_disclaimer(ticker_id):
    """UIがスクリーニングの入力のように見せてしまう事故を防ぐための断り書き。

    表を分けてあるというサーバ側の事情は、JSONだけを見る利用者(自作の
    スクリプト等)には伝わらない。だからレスポンスにも必ず載せる。
    """
    for path in (f"/api/v1/llm/{_SYMBOL}", "/api/v1/llm/report"):
        body = client.get(path).json()
        assert body["advisory"] is True
        assert body["disclaimer"] == LLM_DISCLAIMER


def test_ticker_without_any_analysis_returns_empty_not_404(ticker_id):
    """未生成は正常な状態。生成には課金が伴うので、**作っていないのが既定**。"""
    response = client.get(f"/api/v1/llm/{_SYMBOL}")
    assert response.status_code == 200
    body = response.json()
    assert body["summaries"] == []
    assert body["qualitative"] is None


def test_unknown_ticker_is_404(ticker_id):
    """銘柄自体が無いのは別の話——こちらは本当に存在しないので404。"""
    assert client.get("/api/v1/llm/ZZNOPE9").status_code == 404


def test_only_the_latest_qualitative_is_returned(ticker_id):
    """定性評価は最新1件だけ返す(どれが今の見解かを曖昧にしない)。"""
    _add_qualitative(ticker_id, as_of=_AS_OF - datetime.timedelta(days=30), conviction="low")
    _add_qualitative(ticker_id, as_of=_AS_OF, conviction="high")

    body = client.get(f"/api/v1/llm/{_SYMBOL}").json()
    assert body["qualitative"]["conviction"] == "high"


# ---------------------------------------------------------------------------
# GET /llm/report
# ---------------------------------------------------------------------------


def test_report_returns_content_and_ranked_symbols(ticker_id):
    _add_report()

    body = client.get("/api/v1/llm/report").json()

    assert body["exists"] is True
    assert body["score_date"] == _SCORE_DATE.isoformat()
    assert body["content"].startswith("### 今日の全体像")
    assert body["ranked_symbols"] == [_SYMBOL]


def test_report_can_be_fetched_by_score_date(ticker_id):
    _add_report()
    assert client.get(f"/api/v1/llm/report?date={_SCORE_DATE.isoformat()}").json()["exists"] is True
    # 別の日を指定したらヒットしない(最新へフォールバックしない)。
    assert client.get("/api/v1/llm/report?date=2098-01-01").json()["exists"] is False


def test_report_path_is_not_swallowed_by_the_ticker_route():
    """`/llm/report` が `/llm/{ticker}` に食われないこと。

    "REPORT" は TICKER_PATTERN に一致するため、宣言順を間違えると
    「REPORT という銘柄」を探して404になる。順序はコードの見た目に現れない
    ので、テストで固定しておく。
    """
    response = client.get("/api/v1/llm/report")
    assert response.status_code == 200
    assert "exists" in response.json()


# ---------------------------------------------------------------------------
# GET /llm/providers
# ---------------------------------------------------------------------------


def test_providers_lists_anthropic_and_openai_compat():
    body = client.get("/api/v1/llm/providers").json()
    names = {p["provider"] for p in body["providers"]}
    assert names == {"anthropic", "openai_compat"}
    assert body["current"] in names
    for p in body["providers"]:
        assert isinstance(p["configured"], bool)
        assert p["suggested_models"]  # UIのプルダウンが空にならない


# ---------------------------------------------------------------------------
# POST /llm/report/generate — API層で唯一の書き込み(課金あり)
# ---------------------------------------------------------------------------


@pytest.fixture
def _reset_rate_limit():
    """生成のレート制限はモジュール global。テスト間で持ち越さない。"""
    routes._report_gen_last_at = 0.0
    yield
    routes._report_gen_last_at = 0.0
    _cleanup()


@pytest.fixture
def _stub_provider(monkeypatch):
    """`build_provider` を差し替え、APIキーの有無に関係なくルートを通す。"""
    monkeypatch.setattr("autoscreener.llm.client.build_provider", lambda cfg=None: object())


def _fake_generate_ok(**kwargs):
    """実LLMを叩かず、`generate-report` が1行書いたのと同じ状態を作る。"""
    with session_scope() as session:
        session.add(
            LlmAnalysis(
                ticker_id=None,
                kind="daily_report",
                source_key=_SCORE_DATE.isoformat(),
                as_of=_AS_OF,
                model=kwargs.get("config").model if kwargs.get("config") else "claude-opus-5",
                effort="high",
                prompt_fingerprint="c" * 64,
                content="### 今日の全体像\nUIから生成した。",
                source_refs={"as_of": _SCORE_DATE.isoformat(), "ranked_symbols": []},
            )
        )
    return {"candidates": 1, "new_rows": 1, "existing": 0, "failures": 0}


def test_generate_requires_confirm(_reset_rate_limit):
    r = client.post("/api/v1/llm/report/generate", json={"confirm": False})
    assert r.status_code == 400


def test_generate_writes_and_returns_the_report(_reset_rate_limit, _stub_provider, monkeypatch):
    monkeypatch.setattr(gen_mod, "generate_report", _fake_generate_ok)
    r = client.post(
        "/api/v1/llm/report/generate",
        json={"confirm": True, "score_date": _SCORE_DATE.isoformat(), "model": "claude-sonnet-5"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["report"]["exists"] is True
    assert body["report"]["content"].startswith("### 今日の全体像")


def test_generate_is_rate_limited_within_the_window(_reset_rate_limit, _stub_provider, monkeypatch):
    monkeypatch.setattr(gen_mod, "generate_report", _fake_generate_ok)
    first = client.post("/api/v1/llm/report/generate", json={"confirm": True})
    assert first.status_code == 200
    second = client.post("/api/v1/llm/report/generate", json={"confirm": True})
    assert second.status_code == 429
    assert "Retry-After" in second.headers


def test_generate_reports_409_when_provider_is_unavailable(_reset_rate_limit, monkeypatch):
    """`generate_report` は内部で LlmDisabled を握って0件で終わるので、UIに
    「未設定」を伝えるにはルート側が `build_provider` の時点で捕まえる必要がある。"""

    def _disabled(cfg=None):
        raise LlmDisabled("OPENAI_API_KEY 未設定")

    monkeypatch.setattr("autoscreener.llm.client.build_provider", _disabled)
    r = client.post(
        "/api/v1/llm/report/generate", json={"confirm": True, "provider": "openai_compat"}
    )
    assert r.status_code == 409


def test_generate_rejects_unknown_provider(_reset_rate_limit):
    r = client.post(
        "/api/v1/llm/report/generate", json={"confirm": True, "provider": "not-a-provider"}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /llm/connections — 名前付き接続プロファイルの一覧・作成・編集・削除・切替
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_connections():
    """llm_connections をテスト前後で丸ごと退避・復元する(名前空間が無いため)。"""
    from autoscreener.db.models import LlmConnection

    cols = ("name", "provider", "base_url", "model", "effort", "send_effort", "api_key", "is_active")
    with session_scope() as session:
        saved = [{c: getattr(r, c) for c in cols} for r in session.query(LlmConnection).all()]
        session.query(LlmConnection).delete()
    yield
    with session_scope() as session:
        session.query(LlmConnection).delete()
        for row in saved:
            session.add(LlmConnection(**row))


def _create(name: str, **over):
    payload = {"name": name, "provider": "openai_compat", "base_url": "http://x/v1", **over}
    return client.post("/api/v1/llm/connections", json=payload)


def test_connection_create_list_hides_the_key_body(_clean_connections):
    r = _create("local-ollama", model="llama3.1:70b", api_key="sk-secret")
    assert r.status_code == 201, r.text
    row = r.json()
    assert "api_key" not in row and row["api_key_set"] is True
    assert row["name"] == "local-ollama" and row["is_active"] is False

    listing = client.get("/api/v1/llm/connections").json()["connections"]
    assert [c["name"] for c in listing] == ["local-ollama"]
    assert "sk-secret" not in client.get("/api/v1/llm/connections").text


def test_connection_name_must_be_unique(_clean_connections):
    assert _create("dup").status_code == 201
    assert _create("dup").status_code == 409


def test_connection_activate_is_exclusive_and_drives_settings(_clean_connections):
    a = _create("nim", provider="openai_compat", base_url="https://nim/v1",
                model="meta/llama-3.1-70b-instruct", api_key="nv-key").json()
    b = _create("claude", provider="anthropic", base_url=None, api_key="sk-ant").json()

    assert client.post(f"/api/v1/llm/connections/{a['id']}/activate").status_code == 200
    s = client.get("/api/v1/llm/settings").json()
    assert s["provider"] == "openai_compat" and s["base_url"] == "https://nim/v1"
    assert s["model"] == "meta/llama-3.1-70b-instruct"
    assert s["active_connection_name"] == "nim"
    assert s["openai_api_key_set"] is True

    # 別を有効化すると前のは自動で下りる。
    client.post(f"/api/v1/llm/connections/{b['id']}/activate")
    listing = {c["name"]: c["is_active"] for c in client.get("/api/v1/llm/connections").json()["connections"]}
    assert listing == {"nim": False, "claude": True}

    # 解除すると collection.yaml / .env に戻る。
    client.post("/api/v1/llm/connections/deactivate")
    assert client.get("/api/v1/llm/settings").json()["active_connection_id"] is None


def test_connection_update_edits_fields_and_clears_with_empty_string(_clean_connections):
    cid = _create("edit-me", model="m1", api_key="k1").json()["id"]
    r = client.put(f"/api/v1/llm/connections/{cid}", json={"model": "m2", "base_url": ""})
    assert r.status_code == 200
    row = r.json()
    assert row["model"] == "m2"
    assert row["base_url"] is None  # "" でクリア
    assert row["api_key_set"] is True  # 触っていないので残る
    # api_key に "" で削除。
    assert client.put(f"/api/v1/llm/connections/{cid}", json={"api_key": ""}).json()["api_key_set"] is False


def test_connection_update_rejects_invalid_provider(_clean_connections):
    cid = _create("x").json()["id"]
    assert client.put(f"/api/v1/llm/connections/{cid}", json={"provider": "bogus"}).status_code == 422


def test_connection_delete_removes_it(_clean_connections):
    cid = _create("gone").json()["id"]
    assert client.delete(f"/api/v1/llm/connections/{cid}").status_code == 204
    assert client.get("/api/v1/llm/connections").json()["connections"] == []
    assert client.delete(f"/api/v1/llm/connections/{cid}").status_code == 404


def test_generate_uses_the_active_connection(_reset_rate_limit, _clean_connections, monkeypatch):
    """アクティブなプロファイルの provider/model が generate に渡る。"""
    _create("nim", provider="openai_compat", base_url="https://nim/v1",
            model="meta/llama-3.1-70b-instruct", api_key="nv-key", activate=True)

    captured = {}

    def _fake(**kwargs):
        captured["cfg"] = kwargs.get("config")
        return {"candidates": 0, "new_rows": 0, "existing": 0, "failures": 0}

    monkeypatch.setattr("autoscreener.llm.client.build_provider", lambda cfg=None: object())
    monkeypatch.setattr(gen_mod, "generate_report", _fake)
    r = client.post("/api/v1/llm/report/generate", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert captured["cfg"].provider == "openai_compat"
    assert captured["cfg"].model == "meta/llama-3.1-70b-instruct"
    assert captured["cfg"].base_url == "https://nim/v1"
