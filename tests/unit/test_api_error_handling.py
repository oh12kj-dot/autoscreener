"""APIのエラー応答がCORSヘッダを失わないことのテスト(27.22)。

**なぜこのテストが要るのか**:DBマイグレーション後に古いAPIプロセスが動いた
ままだったとき、全画面が `Failed to fetch` になり、原因(スキーマ不一致による
500)がまったく見えなかった。Starletteの `ServerErrorMiddleware` は最外殻に
あるため、そこが返す素の500にはCORSヘッダが付かず、ブラウザは応答を読む前に
ブロックする。結果、**サーバー停止・500・CORS設定ミスが区別できない**。

ミドルウェアの登録順序に依存する挙動なので、順序を入れ替えた瞬間に静かに
壊れる。テストで固定しておく。
"""

from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from autoscreener.api.main import app

ORIGIN = "http://localhost:5173"

_probe = APIRouter()


@_probe.get("/__test__/boom")
def _boom() -> None:
    raise RuntimeError("simulated failure")


@_probe.get("/__test__/schema")
def _schema() -> None:
    raise ProgrammingError("SELECT 1", {}, Exception('column scores.overall_score does not exist'))


app.include_router(_probe)
client = TestClient(app, raise_server_exceptions=False)


def test_unhandled_error_still_carries_cors_headers():
    """これが無いとブラウザ側は原因不明の `Failed to fetch` にしかならない。"""
    response = client.get("/__test__/boom", headers={"Origin": ORIGIN})
    assert response.status_code == 500
    assert response.headers.get("access-control-allow-origin") == ORIGIN
    assert response.json()["error_type"] == "internal_error"


def test_schema_mismatch_returns_actionable_guidance():
    """最も起きやすい失敗(マイグレーション後の再起動忘れ)に専用メッセージを出す。"""
    response = client.get("/__test__/schema", headers={"Origin": ORIGIN})
    assert response.status_code == 503
    assert response.headers.get("access-control-allow-origin") == ORIGIN
    body = response.json()
    assert body["error_type"] == "schema_mismatch"
    assert "再起動" in body["detail"]


def test_health_does_not_touch_the_database():
    """`/health` はプロセスの生死だけを見る(縮退時も200)。"""
    assert client.get("/health").json() == {"status": "ok"}


def test_ready_confirms_schema_and_code_agree():
    """`/health` が200でも壊れている状態を切り分けるためのエンドポイント。"""
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
