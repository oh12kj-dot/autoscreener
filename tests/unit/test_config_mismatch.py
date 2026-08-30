"""設定スキーマ不一致がAPIでどう見えるかのテスト(28.17)。"""

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError

from autoscreener.api.main import app
from autoscreener.config import ConfigSchemaError


class _OldSchema(BaseModel):
    """v4で削除済みのフィールドを要求する、古いコードを模したモデル。"""

    mean_reversion_weight: float = Field()


def _config_mismatch() -> ConfigSchemaError:
    try:
        _OldSchema.model_validate({})
    except ValidationError as exc:
        return ConfigSchemaError(Path("config/scoring.yaml"), exc)
    raise AssertionError("expected a ValidationError")


def test_ready_reports_config_mismatch_instead_of_claiming_ready():
    """**`/ready` が200なら本当にデータを返せること**が満たすべき性質(28.17)。

    v4移行時、`/ready` はDBしか見ていなかったため200を返し続け、一方でデータを
    返す全エンドポイントが設定スキーマ不一致で500になっていた。
    「readyと言っているのに何も動かない」は切り分けに最も使えない状態である。
    """
    with patch("autoscreener.api.main.load_scoring_config", side_effect=_config_mismatch()):
        response = TestClient(app).get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "config_schema_mismatch"
    assert "mean_reversion_weight" in body["fields"]
    assert "再起動" in body["detail"]


def test_health_stays_up_when_the_config_is_mismatched():
    """`/health` はプロセスの生死だけを見る。切り分けのために両方が要る。"""
    with patch("autoscreener.api.main.load_scoring_config", side_effect=_config_mismatch()):
        response = TestClient(app).get("/health")
    assert response.status_code == 200


def test_data_endpoint_names_the_stale_process_not_the_config_file():
    """素のPydanticエラーは「設定ファイルが壊れている」と読める。原因は逆が多い。

    v4移行時に実際に踏んだ:削除済みフィールドが8件「必須なのに無い」と
    報告され、直すべき場所(=プロセスの再起動)がまったく見えなかった。
    """
    with patch("autoscreener.api.routes.load_scoring_config", side_effect=_config_mismatch()):
        response = TestClient(app, raise_server_exceptions=False).get("/api/v1/candidates")
    assert response.status_code == 503
    body = response.json()
    assert body["error_type"] == "config_schema_mismatch"
    assert "再起動" in body["detail"]
    assert body["fields"] == ["mean_reversion_weight"]


def test_response_validation_failure_is_not_blamed_on_the_config():
    """**設定の不一致と、それ以外の検証エラーを取り違えないこと**(28.17)。

    レスポンスモデルの検証失敗も `ValidationError` で飛んでくる。それを設定
    不一致として扱うと「APIプロセスを再起動してください」という無関係な指示が
    出る。実際に `CandidateDetail.factors` の型不足で見通しマイナス銘柄の詳細が
    失敗したとき、この誤った案内が出ていた。

    原因の違うものに同じ対処を案内するのは、何も案内しないより悪い——利用者は
    再起動を試し、直らず、そこで手がかりを失う。
    """

    def _response_model_failure(*args, **kwargs):
        _OldSchema.model_validate({})

    with patch("autoscreener.api.routes.load_scoring_config", side_effect=_response_model_failure):
        response = TestClient(app, raise_server_exceptions=False).get("/api/v1/candidates")
    assert response.status_code == 500
    body = response.json()
    assert body["error_type"] == "internal_error"
    assert "再起動" not in body["detail"]
