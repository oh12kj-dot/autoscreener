"""FastAPIアプリケーション本体(6.5)。

起動:`uv run uvicorn autoscreener.api.main:app --reload`
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from autoscreener.api.dependencies import get_session
from autoscreener.api.operational_readiness import build_operational_readiness
from autoscreener.api.routes import router
from autoscreener.config import ConfigSchemaError, load_scoring_config
from autoscreener.db.models import Score

logger = logging.getLogger(__name__)

app = FastAPI(title="10bagger candidate screener API", version="v1")


@app.middleware("http")
async def error_envelope(request: Request, call_next):
    """未処理例外を、CORSヘッダの付いたJSONレスポンスに変換する(27.22)。

    **なぜ必要か**:Starletteのミドルウェアは後から追加したものが外側に来る。
    未処理例外を拾う `ServerErrorMiddleware` は常に最外殻にあるため、
    そこが返す素の500レスポンスには **CORSヘッダが付かない**。ブラウザは
    ヘッダの無いクロスオリジン応答を読む前にブロックするので、`fetch()` は
    `TypeError: Failed to fetch` で失敗する——**サーバーが落ちているのか、
    500を返しているのか、CORS設定が違うのかが区別できない**。

    実際、DBマイグレーション後に古いAPIプロセスが動いたままだったとき、
    全画面が「Failed to fetch」になり、原因(スキーマ不一致による500)が
    まったく見えなかった。この関数はCORSミドルウェアの**内側**で登録されるため
    (下の `add_middleware` より先に定義されている)、ここで返すJSONには
    CORSヘッダが付き、フロントエンドが実際の理由を表示できる。

    スキーマ不一致(`ConfigSchemaError`・`ProgrammingError`)だけは、原因と対処が
    定型的なので専用のメッセージにする。運用中に最も起きやすい失敗がこれである。

    **`ValidationError` そのものを拾ってはいけない。** レスポンスモデルの検証
    失敗も同じ例外型で飛んでくるため、それらまで「APIプロセスを再起動して
    ください」と案内してしまう。実際に `CandidateDetail.factors` の型不足で
    見通しマイナス銘柄の詳細が失敗したとき、無関係な再起動を指示していた。
    設定の読み込みだけが `ConfigSchemaError` に包まれている(28.17)。
    """
    try:
        return await call_next(request)
    except ConfigSchemaError as exc:
        # 28.17:設定ファイルとコードのスキーマ不一致。DBスキーマ不一致(下の
        # ProgrammingError)と**まったく同じ形の障害**である——プロセスだけが
        # 古く、ディスク上のファイルは新しい。
        #
        # 素のPydanticエラーをそのまま返すと「Field required」が並ぶだけで、
        # 利用者には**設定ファイルが壊れている**ように読める。実際に多いのは
        # 「コードを更新したのにAPIプロセスを再起動していない」ほうであり、
        # 直すべき場所が正反対になる。v4移行時に実際にこれを踏んだ:
        # `multiple.mean_reversion_weight` など、v4では**削除済みの**フィールドが
        # 8件「必須なのに無い」と報告され、原因が設定側にあるように見えていた。
        logger.exception("config schema mismatch")
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "設定ファイル(config/*.yaml)とAPIのコードのスキーマが一致していません。"
                    "**まず APIプロセスを再起動してください** —— コードを更新したのに古い"
                    "プロセスが動いたままだと必ずこうなります(`--reload` 付きで起動していれば"
                    "自動で読み直されます)。再起動しても直らない場合は、下の項目が"
                    "設定ファイルに正しく書かれているかを確認してください。"
                ),
                "error_type": "config_schema_mismatch",
                "fields": exc.fields,
                "cause": str(exc),
            },
        )
    except ProgrammingError as exc:
        logger.exception("database schema mismatch")
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "DBスキーマとAPIのコードが一致していません。"
                    "`uv run alembic upgrade head` を実行し、**APIプロセスを再起動**してください"
                    "(マイグレーション後に古いプロセスが動いたままだと必ずこうなります)。"
                ),
                "error_type": "schema_mismatch",
                "cause": str(exc.orig) if exc.orig else str(exc),
            },
        )
    except SQLAlchemyError as exc:
        logger.exception("database error")
        return JSONResponse(
            status_code=503,
            content={
                "detail": "データベースに接続できないか、クエリが失敗しました。Postgresが起動しているか確認してください。",
                "error_type": "database_error",
                "cause": str(exc.orig) if getattr(exc, "orig", None) else str(exc),
            },
        )
    except Exception as exc:  # noqa: BLE001 — 最後の砦。ここで握らないとCORSヘッダが失われる
        logger.exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content={"detail": f"サーバー内部エラー: {exc}", "error_type": "internal_error"},
        )


# 個人利用(11.1解釈A)のローカルReact開発サーバーからの呼び出しのみを許可する。
# 外部公開する場合(解釈B/C)はオリジンを絞り込むこと。
#
# **順序が重要**:`add_middleware` は後から追加したものが外側になる。CORSを
# `error_envelope` より後に登録することで CORS が外側に来て、上の関数が返す
# エラーJSONにもCORSヘッダが付く。
#
# GET 以外(POST/PUT/DELETE)は当初許可していなかったが、LLM接続プロファイルの
# 管理(`/api/v1/llm/connections*`)とブラウザからのレポート生成
# (`POST /api/v1/llm/report/generate`)を追加した際に、これらの書き込み系
# リクエストが CORS プリフライト(`OPTIONS`)で `400 Disallowed CORS method` に
# なり、フロントには「APIに接続できません」としか見えなくなった。書き込み系の
# メソッドも許可する(いずれも `Content-Type: application/json` 必須なので単純
# フォーム CSRF はプリフライトで弾かれる)。
#
# **5173決め打ちについて(defect 2, 2026-09-05監査, docs/audit_followup
# _2026-09-05.md)**:ここに列挙したポートは `frontend/vite.config.ts` の
# `server.port`(同じく5173に固定、`strictPort: true`)と手動で一致させている
# 独立した2つのハードコードであり、共有の設定源はない。ポートが5173で
# 埋まっていた場合、Viteの既定動作は「黙って5174に逃げる」で、その5174は
# ここが弾く——ブラウザには汎用の接続エラーしか出ず、原因がポートだとは
# わからない(2026-09-05時点でこのマシン上に5173と5174が同時にLISTENして
# いた、実際に踏んだ事故)。`vite.config.ts` 側は `strictPort: true` にして
# 「黙って5174に移る」代わりに起動を失敗させることで対処済み——このポートを
# 変える場合は **両ファイルを同時に** 変更すること。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """プロセスの生死だけを見る。DBには触らない(縮退時も200を返す)。"""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    """このプロセスが実際に使える状態かを確認する。DBと**設定ファイルの両方**を見る。

    `/health` と分けているのは、この種の障害が「プロセスは生きているが、
    ディスク上のファイルと噛み合っていない」という状態だったため。`/health` は
    200を返し続けるので切り分けに使えない。

    **設定ファイルの検証を含めるのは28.17の教訓である。** v4移行時、`/ready` は
    DBしか見ていなかったため200を返し続け、一方でデータを返す全エンドポイントが
    設定スキーマ不一致で500になっていた。「readyと言っているのに何も動かない」
    という、切り分けに最も使えない状態である。**`/ready` が200を返すなら
    データも返せる**、が満たすべき性質。
    """
    try:
        scoring_version = load_scoring_config().scoring_version
    except ConfigSchemaError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "config_schema_mismatch",
                "detail": (
                    "設定ファイルとこのプロセスのコードが一致していません。"
                    "APIプロセスを再起動してください。"
                ),
                "fields": exc.fields,
            },
        )

    session = next(get_session())
    try:
        latest = session.query(func.max(Score.score_date)).scalar()
        return JSONResponse(
            {
                "status": "ready",
                "scoring_version": scoring_version,
                "latest_score_date": latest.isoformat() if latest else None,
            }
        )
    finally:
        session.close()


@app.get("/operational-readiness")
def operational_readiness() -> JSONResponse:
    """A-5(2026-09-04、docs/racr_wp_a_operational_safety_2026-09-04.md、
    監査§10.3/10.4):日次パイプラインが実際に回っていて、ランキングの元
    データが新しいかを見る。**`/ready` とは別の問いに答える**——`/ready` は
    「このプロセスがDB・設定に噛み合っているか」だけを見る契約のまま変えて
    いない(28.17)。

    2026-09-03のように pipeline が `running` のまま停止していても、DBと
    最新スコアさえ存在すれば `/ready` は200を返し続ける(それ自体は正しい
    ——プロセス自体は健全なので)。運用者・UIが「pipelineは実際に健康か」を
    知るには別の窓口が要る、というのがこのエンドポイントの存在理由。

    判定ロジック本体は `api/operational_readiness.py` にある(main.py を
    肥大させないため)。常に200を返し、`status` フィールド
    (`"ready"`/`"degraded"`)で状態を表す——`/ready` のような可用性
    プローブではなく、状態レポートだからである。
    """
    session = next(get_session())
    try:
        return JSONResponse(build_operational_readiness(session))
    finally:
        session.close()
