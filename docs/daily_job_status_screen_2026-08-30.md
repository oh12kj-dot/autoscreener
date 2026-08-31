# 日次ジョブ実行状況画面 設計書(2026-08-30)

## 0. なぜ必要か(この設計の出発点)

2026-08-29 の実運用ログ(`logs/daily_pipeline_20260829.log`)は、この画面が
無いことの帰結をそのまま示している。

```
daily collection: 0 symbols
ERROR scoring.engine: run_scoring aborted: insufficient_price_coverage
      (0.1% of 1260 gated tickers have a 2026-08-26 price row, need 90%)
ERROR batch.daily_pipeline: collect_filings failed (EDGAR_USER_AGENT not set)
ERROR monitoring: quarantine ratio critically high: 100.0% (5312/5312)
==== end (exit code 0) ====
```

ユニバース全 5312 銘柄が隔離され、収集対象が 0 件になり、スコアリングが中断し、
提出書類収集が例外で落ちた。それでも **終了コードは 0**、UI は前日のランキングを
何事もなかったように表示し続ける。18.4 の「縮退運用(前回成功分をそのまま返す)」は
仕様どおりに働いているが、**縮退していることを利用者に伝える面が存在しない**。

現状 UI が持つのは `CollectionStatusBanner`(収集が実行中のときだけ N/M 件を出す)
のみで、これは 15 工程あるパイプラインのうち 1 工程の進捗しか映さない。要件 14.15
「運用監視(未考慮)— ジョブ失敗・成功率低下のアラート」はここが未着手であることを
既に認めている。本設計はそこを埋める。

### 設計の芯となる要求

**「終了コード 0」と「正常」を同一視しない。** この画面が答えるべき問いは
「ジョブは落ちたか」ではなく「**今表示されているランキングは、今日のデータで
作られたものか**」である。工程が例外なく完走した上で成果が 0 件、という
2026-08-29 型の失敗こそが主対象であり、例外の表示はその副産物にすぎない。

---

## 1. 現状のギャップ(実装前に把握すべき事実)

| 事実 | 出典 | 帰結 |
|---|---|---|
| パイプラインの結果 `dict[str, dict[str, int]]` はプロセス終了時に消える | `batch/daily_pipeline.py` は戻り値を返すだけ、CLI が stdout に出して終わり | 過去の実行を後から参照する手段がない |
| 15 工程中 9 工程が `try/except` + `logger.exception` で握り潰される | `daily_pipeline.py`(cik_map/macro/xbrl/events/supply/backtest/filings/monitoring/backup) | 失敗が DB に一切残らない。ログファイルを開く以外に知る方法がない |
| 永続化されているのは収集工程のみ | `collection_logs`(`run_started`/`run_finished` マーカー含む) | ゲート・スコアリング・バックアップ等の実行有無は追跡不能 |
| `monitoring.py` の健全性判定は戻り値なし(`-> None`) | `check_collection_health` / `check_quarantine_health` | 判定結果が WARNING ログにしか残らず、API から読めない |
| `insider` と `short_interest` が単一の `try` ブロックを共有 | `daily_pipeline.py` の週次ブロック | `collect_insider()` が落ちると `collect_short_interest()` は実行すらされないのに、ログ上は区別できない |
| Alembic head | `f9b2d6e1a3c7`(J-7 supply tables) | 新規マイグレーションの `down_revision` に指定する |

**結論:画面を作る前に、記録する層を作る必要がある。** フロントだけでは実装不能。

---

## 2. スコープ

### やること
1. `pipeline_runs` / `pipeline_stage_runs` 2 テーブルの追加(+ Alembic)
2. `daily_pipeline.py` を工程ごとに記録するよう改修(**工程の実行順序・成否の挙動は変えない**)
3. `monitoring.py` の健全性判定を構造化して返すよう変更(ログ出力は維持)
4. API 2 エンドポイント追加
5. フロント新規ページ `/pipeline`「日次ジョブ」
6. ユニットテスト

### やらないこと(意図的)
- **過去実行のバックフィル。** `collection_logs` から収集工程だけ再構成することは
  技術的には可能だが、他 14 工程は復元できない。**半分だけ埋まった履歴は
  「その日は他の工程が動かなかった」という誤読を生む。** 履歴は本実装以降の
  実行から積み上げ、それ以前は「記録なし」と明示する。B-6 の
  `collection_complete: None`(不明を False にしない)と同じ判断。
- 画面からのジョブ再実行・キャンセル。API は読み取り専用(18.6)。運用は CLI と
  Task Scheduler に置く。
- 通知(メール/デスクトップ)。18.7 の解釈どおりログ出力に留める。
- リアルタイム push。ポーリングで足りる(§6.4)。

---

## 3. データモデル

### 3.1 `pipeline_runs`(パイプライン 1 回の実行 = 1 行)

```python
class PipelineRun(Base):
    """日次パイプライン1回分の実行記録(14.15の運用監視)。

    `collection_logs` は収集工程の**銘柄単位**のログであり、パイプライン全体の
    実行単位ではない。両者は別の粒度なので別テーブルにする(collection_logs の
    run_id とは無関係な独立した uuid を持つ)。
    """
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(unique=True, index=True)
    run_date: Mapped[datetime.date] = mapped_column(Date, index=True)   # utc_today()
    is_weekly: Mapped[bool] = mapped_column(default=False)              # 月曜=週次工程あり
    trigger: Mapped[str] = mapped_column(String(20))                    # "scheduled" / "manual"
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    # NULL = 実行中、またはプロセスが強制終了した(§4.3 の孤児判定)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "running" / "succeeded" / "degraded" / "failed"(§3.3)
    status: Mapped[str] = mapped_column(String(20), index=True)
    # §3.4 の健全性所見。空リストなら所見なし
    health: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

### 3.2 `pipeline_stage_runs`(工程 1 つ = 1 行)

```python
class PipelineStageRun(Base):
    __tablename__ = "pipeline_stage_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "stage", name="uq_stage_run_stage"),
        Index("ix_pipeline_stage_runs_run_seq", "run_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(40))   # "collection" / "gates" / ... (§3.5)
    sequence: Mapped[int] = mapped_column()          # 実行順(表示順を DB 側で決める)
    # "running" / "succeeded" / "failed" / "skipped"(§3.3)
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 工程の戻り値(件数の dict)。失敗時は None
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # skipped の理由 "not_weekly" 等 / failed の例外クラス名
    reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)   # str(exc) 先頭 2000 字
    error_traceback: Mapped[str | None] = mapped_column(Text, nullable=True) # 先頭 8000 字
```

> **`ondelete="CASCADE"` を付ける理由**:履歴の刈り込み(§4.4)を
> `pipeline_runs` の DELETE 一発で済ませるため。

### 3.3 ステータスの定義 — **3 値ではなく 4 値にすること**

この画面の価値の全ては、ここを潰さないことにかかっている。既存コードの
27.20(「ランキング外を1つの数にまとめない」)と同じ判断を運用面に適用する。

**工程(`pipeline_stage_runs.status`)**

| 値 | 意味 | 例 |
|---|---|---|
| `running` | 開始したが未完了 | 実行中、またはプロセスが死んだ(§4.3) |
| `succeeded` | 例外なく完了 | 成果が 0 件でも succeeded。判断は run 側の health に委ねる |
| `failed` | 例外で終了 | `collect_filings` の `ValueError` |
| `skipped` | 実行対象外 | 火〜日の週次工程(`reason="not_weekly"`) |

**`skipped` を `failed` に混ぜないこと。** 火曜に週次工程が動かないのは正常であり、
それを異常色で出すと画面が毎日 8 個の警告を出して誰も見なくなる。

**実行全体(`pipeline_runs.status`)**

| 値 | 条件 |
|---|---|
| `running` | `finished_at IS NULL` かつ孤児でない |
| `failed` | 中核工程(`collection` / `gates` / `scoring` / `forward_validation`)が failed、または孤児 |
| `degraded` | failed ではないが、任意の工程が failed、または health 所見が 1 件以上 |
| `succeeded` | 上記いずれでもない |

**`degraded` がこの設計の要**。2026-08-29 の実行はこれに落ちる
(全工程が例外なく完走 → `succeeded` に見えるが、health 所見が 3 件立つ)。

### 3.4 健全性所見(`pipeline_runs.health`)

`monitoring.py` の判定を**構造化して返す**よう変更する。ログ出力(18.7)は
現状のまま残し、戻り値を追加するだけ。閾値・判定ロジックは 1 行も変えない
(E-2 の `sanitized` 扱いを含め、根拠のある値なので触らない)。

```python
@dataclass(frozen=True)
class HealthFinding:
    code: str        # "collection_success_rate_low" 等
    severity: str    # "warning" / "error"
    message: str     # 既存のログ文言をそのまま使う
    detail: dict     # {"success_rate": 0.81, "success": 4300, "total": 5312}

def check_collection_health(status_counts: dict[str, int]) -> list[HealthFinding]: ...
def check_quarantine_health(quarantined: int, universe_size: int) -> list[HealthFinding]: ...
```

**既存の 4 判定(署名変更のみ、ロジック不変)**

| code | severity | 既存の閾値 |
|---|---|---|
| `collection_success_rate_low` | error / warning | 0.90 / 0.95 |
| `sanitized_ratio_elevated` | warning | 0.30 |
| `quarantine_ratio_high` | error / warning | 0.10 / 0.05 |

**新規に追加する判定(`monitoring.py` に追加、`check_pipeline_health()` として)**

2026-08-29 型の「静かな失敗」はどれも既存 3 判定に引っかからない。以下を足す。

| code | severity | 条件 | 理由 |
|---|---|---|---|
| `collection_target_empty` | error | 収集対象が 0 件、かつユニバースが 0 件でない | 08-29 の主症状。全銘柄隔離でも成功率判定は total=0 で早期 return するため既存判定を素通りする |
| `scoring_skipped` | error | `scoring` の result に `skipped_reason` が入っている | ランキングが更新されていない。利用者に最も直接影響する |
| `scoring_yield_dropped` | warning | `scored` が前回実行の 50% 未満(前回 scored > 0 のときのみ) | 静かな劣化の検出。初回実行・前回 0 件では判定しない |
| `stage_failed` | warning | 非中核工程が failed(工程ごとに 1 件) | filings/backup/macro 等の失敗を run レベルに集約する |

> `scoring_yield_dropped` は**前回実行との比較**が要るので、判定は
> `run_daily_pipeline` の末尾(全工程完了後)に `pipeline_runs` を 1 件遡って行う。

### 3.5 工程の一覧(`daily_pipeline.py` の実行順どおり)

`sequence` はこの表の番号をそのまま使う。`stage` 文字列は現行の
`results` dict のキーと一致させる(移行が読みやすい)。

| seq | stage | 頻度 | 現状の失敗時挙動 | 中核 |
|---:|---|---|---|:---:|
| 1 | `universe_refresh` | 週次(月) | 例外で全体停止 | |
| 2 | `cik_map_refresh` | 週次(月) | 握り潰し | |
| 3 | `macro` | 週次(月) | 握り潰し | |
| 4 | `xbrl_facts` | 週次(月) | 握り潰し | |
| 5 | `events` | 週次(月) | 握り潰し | |
| 6 | `insider` | 週次(月) | 握り潰し(※) | |
| 7 | `short_interest` | 週次(月) | 握り潰し(※) | |
| 8 | `collection` | 日次 | 例外で全体停止 | ✓ |
| 9 | `gates` | 日次 | 例外で全体停止 | ✓ |
| 10 | `backtest` | 週次(月) | 握り潰し | |
| 11 | `scoring` | 日次 | 例外で全体停止 | ✓ |
| 12 | `forward_validation` | 日次 | 例外で全体停止 | ✓ |
| 13 | `filings` | 日次 | 握り潰し | |
| 14 | `monitoring` | 日次 | 握り潰し | |
| 15 | `backup` | 日次 | 握り潰し | |

> ※ **現状 seq 6 と 7 は単一の `try` を共有している。** `collect_insider()` が
> 落ちると `collect_short_interest()` は実行されないのに、ログ上は両方が
> 一括で失敗したようにしか見えない。記録を工程単位にする以上、
> **この try は 2 つに分割すること**(挙動の改善だが、記録の正確さのために必要)。

---

## 4. バックエンド実装

### 4.1 記録ヘルパ(`src/autoscreener/batch/pipeline_recorder.py` 新規)

```python
class PipelineRecorder:
    """パイプラインの工程を1つずつ記録する。

    記録は**工程ごとに即コミット**する。全部終わってからまとめて書くと、
    プロセスが途中で死んだ実行が丸ごと消え——それは 2026-08-29 型の障害で
    最も知りたい情報そのものである。session_scope() を工程ごとに開き、
    パイプライン本体の長大なトランザクションとは独立させる。
    """
    def __init__(self, run_date: date, is_weekly: bool, trigger: str) -> None: ...

    @contextmanager
    def stage(self, name: str, sequence: int) -> Iterator[StageHandle]:
        """開始時に status="running" で行を作り、終了時に確定する。

        - 正常終了: handle.result に入った dict を保存し succeeded
        - 例外送出: failed + 例外クラス名・メッセージ・traceback を保存し、
          **例外は再送出する**(現行の try/except の位置は呼び出し側に残す。
          このヘルパは記録だけを担い、握り潰すか否かの判断は変えない)
        """

    def skip(self, name: str, sequence: int, reason: str) -> None: ...
    def finish(self, health: list[HealthFinding]) -> None: ...
```

**呼び出し側の書き換え例**(現行の握り潰し構造を保つこと):

```python
# 変更前
logger.info("weekly macro collection")
try:
    results["macro"] = collect_macro()
except Exception:
    logger.exception("weekly macro collection failed (FRED_API_KEY not set?)")

# 変更後
logger.info("weekly macro collection")
try:
    with recorder.stage("macro", 3) as st:
        st.result = results["macro"] = collect_macro()
except Exception:
    logger.exception("weekly macro collection failed (FRED_API_KEY not set?)")
```

月曜以外は週次工程を `recorder.skip("macro", 3, "not_weekly")` で記録する
(既存の `if today.weekday() == WEEKLY_REFRESH_WEEKDAY:` ブロックに `else:` を足す)。

### 4.2 `run_daily_pipeline()` の戻り値

**現行の `dict[str, dict[str, int]]` の戻り値は変えない。** CLI
(`cli.py:487` `run_daily_pipeline_cmd`)がこれを整形して出力しており、
既存テストも依存している。記録は副作用として追加する。

### 4.3 孤児実行(プロセスが死んだ場合)の判定

Task Scheduler がタイムアウトで殺す、Docker が落ちる等で `finished_at` が
NULL のまま残る行が出る。**API 側で判定**し、DB は書き換えない(死亡を
検知する主体が存在しないため、状態を「修復」すると嘘になりうる)。

```python
ORPHAN_THRESHOLD = timedelta(hours=6)  # 8.1 の想定所要時間の十分な上振れ
# finished_at IS NULL かつ started_at < now() - 6h → API 応答では status="failed",
# health に {"code": "run_orphaned", "severity": "error"} を合成して返す
```

### 4.4 履歴の刈り込み

`backup` 工程の直後に、`run_date < today - 180日` の `pipeline_runs` を DELETE
(`pipeline_stage_runs` は CASCADE)。1 日 1 実行 × 16 行なので放置しても
問題になる量ではないが、無限に伸びる表は作らない。

### 4.5 マイグレーション

`alembic revision -m "daily job status: pipeline_runs and stage_runs"`、
`down_revision = "f9b2d6e1a3c7"`。

---

## 5. API

`src/autoscreener/api/routes.py` に追加。既存 `/universe/status` は
**変更しない**(`CollectionStatusBanner` が依存している)。

### 5.1 `GET /api/v1/pipeline/runs?limit=14`

一覧。履歴ストリップと最新実行のヘッダに使う。

```jsonc
{
  "runs": [
    {
      "run_id": "3fb17745-…",
      "run_date": "2026-08-29",
      "is_weekly": false,
      "trigger": "scheduled",
      "started_at": "2026-08-29T00:00:02Z",
      "finished_at": "2026-08-29T00:02:43Z",
      "duration_seconds": 161,
      "status": "degraded",
      "health": [
        {"code": "collection_target_empty", "severity": "error",
         "message": "収集対象が0件でした(ユニバース5312銘柄すべてが隔離中)",
         "detail": {"universe_size": 5312, "quarantined": 5312}}
      ],
      // 履歴ストリップで折れ線にする主要成果。工程の result から抽出
      "headline": {"collected": 0, "gated_in": 1260, "scored": 0},
      "stage_summary": {"succeeded": 6, "failed": 1, "skipped": 8, "running": 0}
    }
  ],
  // 本実装以前の実行は記録が無いことを画面が明示するための旗
  "history_starts_at": "2026-08-30"
}
```

### 5.2 `GET /api/v1/pipeline/runs/{run_id}`

工程の詳細。`latest` を `run_id` の代わりに受け付ける
(`GET /api/v1/pipeline/runs/latest` → 最新実行。初回描画で 2 往復させないため)。

```jsonc
{
  "run": { /* 5.1 と同じ形 */ },
  "stages": [
    {"stage": "universe_refresh", "sequence": 1, "status": "skipped",
     "reason": "not_weekly", "started_at": null, "finished_at": null,
     "duration_seconds": null, "result": null,
     "error_message": null, "error_traceback": null},
    {"stage": "collection", "sequence": 8, "status": "succeeded",
     "duration_seconds": 0.4, "result": {}},
    {"stage": "filings", "sequence": 13, "status": "failed",
     "reason": "ValueError", "duration_seconds": 0.0, "result": null,
     "error_message": "EDGAR_USER_AGENT が未設定です。…",
     "error_traceback": "Traceback (most recent call last):\n…"}
  ]
}
```

**`error_traceback` を API で返してよいか**:このアプリは個人利用・
ローカル実行(11.1 解釈 A)で、API は読み取り専用ロール(18.6)。
トレースバックは運用者本人が読むためのものであり、返す。ただし
**8000 字で切る**(§3.2)。

### 5.3 スキーマ

`api/schemas.py` に `PipelineRunSummary` / `PipelineHealthFinding` /
`PipelineStageView` / `PipelineRunListResponse` / `PipelineRunDetail` を追加。
フロントの `frontend/src/api/types.ts` に対応する interface を写す。

---

## 6. 画面設計:`/pipeline`「日次ジョブ」

`frontend/src/pages/PipelinePage.tsx` を新規作成し、`App.tsx` にルート追加、
`Layout.tsx` のナビ末尾(「モデルの検証状況」の後)に
`<NavLink to="/pipeline">日次ジョブ</NavLink>`。

> ナビ末尾に置く理由:日常的に見る画面ではない。**平常時は見なくてよく、
> 異常時にだけ見に行く**画面として設計する。だからこそ §6.5 の
> 全画面バナーで「見に行くべき日」を知らせる必要がある。

### 6.1 レイアウト全体

```
┌──────────────────────────────────────────────────────────────┐
│ 日次ジョブ                                                    │
│                                                              │
│ ┌─ A. 最新実行サマリ ────────────────────────────────────┐  │
│ │ ● 要注意    2026-08-29 09:00 JST(21時間前)  所要 2分41秒│  │
│ │                                                          │  │
│ │ ⚠ 収集対象が0件でした(ユニバース5312銘柄すべてが隔離中)  │  │
│ │ ⚠ スコアリングが中断しました:価格データの網羅率不足       │  │
│ │   (1260銘柄中0.1%しか2026-08-26の価格行がない/必要90%)   │  │
│ │ ⚠ 提出書類の収集に失敗:EDGAR_USER_AGENT が未設定         │  │
│ │                                                          │  │
│ │ → 表示中のランキングは 2026-08-26 時点のものです          │  │
│ └──────────────────────────────────────────────────────────┘  │
│                                                              │
│ ┌─ B. 成果の推移 ────────────────────────────────────────┐  │
│ │  収集       ゲート通過    スコア付与    隔離率           │  │
│ │  0          1,260         0            100.0%           │  │
│ │  ▼5,287     →0            ▼1,204        ▲94.8pt         │  │
│ └──────────────────────────────────────────────────────────┘  │
│                                                              │
│ ┌─ C. 工程 ──────────────────────────────────────────────┐  │
│ │ ✓ 8  データ収集          0.4秒   0件処理               │  │
│ │ ✓ 9  除外ゲート適用     58.8秒   通過1,260 / 除外4,027  │  │
│ │ − 10 バックテスト(週次) ―       本日は対象外(火曜)    │  │
│ │ ! 11 スコアリング        0.1秒   中断:価格網羅率不足 ▸  │  │
│ │ ✓ 12 前方検証            0.0秒   算出0件                │  │
│ │ ✕ 13 提出書類収集        0.0秒   ValueError          ▸  │  │
│ │ ✓ 14 四半期モニタリング  0.1秒   新規アラート0件         │  │
│ │ ✓ 15 バックアップ       99.9秒   180.7MB 書き出し        │  │
│ │ ─────────── 週次工程(本日は対象外)8件 ▸ ───────────  │  │
│ └──────────────────────────────────────────────────────────┘  │
│                                                              │
│ ┌─ D. 実行履歴(直近14回)───────────────────────────────┐  │
│ │ 日付  状態  所要   収集    通過    スコア                │  │
│ │ 08-29  ●   2:41       0   1,260        0                │  │
│ │ 08-28  ●   —      5,287   1,258    1,204                │  │
│ │ …                                                        │  │
│ └──────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 6.2 A. 最新実行サマリ

- 状態バッジ 4 値。**色だけに意味を載せない**(記号 + 語を必ず併記):
  `● 正常` / `● 要注意`(degraded) / `● 失敗`(failed) / `◐ 実行中`(running)
- 時刻は「絶対時刻 + 相対」の併記。相対だけだと「1日前」が
  今朝の実行なのか昨日の失敗なのか分からない。
- **health 所見を人間の文にして並べる。** ここが画面の主役。
  `message` はバックエンドの既存ログ文言をそのまま出すのではなく、
  `code` に対応する**日本語の説明文をフロントで持つ**
  (`frontend/src/pipelineHealth.ts`)。既存の `warnings.ts` /
  `dueDiligence.ts` と同じ流儀。
- 最下段の「表示中のランキングは YYYY-MM-DD 時点のものです」は、
  `scoring` が `succeeded` かつ `skipped_reason` なしのときは出さない。
  日付は最新 `scores.score_date`(`GET /candidates` が使う日付と一致させること)。
- **`running` のとき**:所見の代わりに「実行中(N/15 工程完了)」を出し、
  §6.4 のポーリングを有効化する。

### 6.3 B. 成果の推移 / C. 工程 / D. 実行履歴

**B. 成果タイル(4 枚)**
`collected` / `gated_in` / `scored` / `quarantine_ratio`。各タイルに
**前回実行比のデルタ**を付ける。`0` という数字は単体では読めず、
「昨日 5,287 → 今日 0」で初めて異常と分かる。前回実行が無ければデルタは
`—`(0 と書かない)。

**C. 工程リスト**
- `pipeline_stage_runs` を `sequence` 順に並べる。番号を出す(ログと突き合わせやすい)。
- 状態記号:`✓` succeeded / `✕` failed / `!` succeeded だが health 所見が
  紐づく / `−` skipped / `◐` running。
- **`skipped` の週次工程は既定で畳む。** 火〜日は 8 件が skipped になり、
  展開したままでは日次工程が埋もれる。月曜(`is_weekly: true`)は既定で展開。
- `result` の dict は**そのまま JSON を出さず**、工程ごとの整形関数で
  「通過1,260 / 除外4,027」のように書く(`frontend/src/pipelineStages.ts`)。
  未知のキーはフォールバックで `key: value` 表示。
- `▸` で行を展開 → `error_traceback` を `<pre>` で表示(等幅・横スクロール可)。
  失敗行のみ展開可能。

**D. 実行履歴(直近 14 回)**
- 各行クリックで C. がその実行の内容に切り替わる(URL は `/pipeline?run=<uuid>`)。
- `history_starts_at` より前は行を作らず、表の末尾に
  「これより前の実行は記録がありません(記録開始:2026-08-30)」と出す。
  **空行やゼロで埋めない**(§2 の「やらないこと」の理由そのもの)。

### 6.4 データ取得

- 初期表示:`GET /api/v1/pipeline/runs?limit=14` と
  `GET /api/v1/pipeline/runs/latest` を並列に投げる。
- **`status === "running"` のときのみ 15 秒間隔でポーリング**。
  それ以外は再取得しない(日次ジョブの状態は 1 日 1 回しか変わらない)。
  タブ非表示時は `document.hidden` で停止。
- 失敗時は `ErrorBoundary` に任せず、ページ内に
  「実行状況を取得できませんでした(API が停止している可能性があります)」を出す。
  **この画面自身が API 障害を映す場でもある**ため、白紙で終わらせない。

### 6.5 `CollectionStatusBanner` の拡張(重要)

**新規ページを作るだけでは 2026-08-29 は防げない。** 利用者は
ランキング画面しか開かないからである。既存バナーを拡張する:

| 条件 | 表示 |
|---|---|
| 最新 run が `running` | 現行どおり「本日の収集を実行中です (N/M件)」 |
| 最新 run が `failed` または `degraded` | **新規**:「⚠ 本日の日次ジョブに問題がありました。表示中のランキングは YYYY-MM-DD 時点のものです。→ 詳細」(`/pipeline` へのリンク) |
| 最新 run が `succeeded` | 何も出さない(現行どおり邪魔をしない) |
| 最新 run が今日の日付でない | 「⚠ 本日の日次ジョブがまだ実行されていません(最終実行:MM-DD)」 |

データ源を `/universe/status` から `/api/v1/pipeline/runs?limit=1` に切り替える。
**既存の「実行中 N/M 件」表示は残す**(取得元を差し替えて同じ文言を出す)。

### 6.6 CSS

`frontend/src/index.css` に追記。既存の `.collection-status-banner` /
`.collection-status-dot` の命名に揃え、`.pipeline-*` 接頭辞を使う。
既存の配色トークンを流用し、新しい色を導入しない。

---

## 7. テスト

`tests/unit/test_pipeline_recorder.py`(新規)
- 工程の正常終了 → `succeeded` + `result` が保存される
- 工程が例外送出 → `failed` + `reason`(例外クラス名)+ traceback が保存され、
  **例外が再送出される**
- `skip()` → `skipped` + `reason="not_weekly"`
- 工程ごとに即コミットされる(未完了の run を別セッションから読める)

`tests/unit/test_pipeline_health.py`(新規)
- **2026-08-29 の再現:** 全工程 succeeded・collection 0 件・scoring に
  `skipped_reason` あり・隔離率 100% → run status が `degraded`、
  health に `collection_target_empty` / `scoring_skipped` /
  `quarantine_ratio_high` の 3 件。**このテストがこの実装の存在理由である。**
- `scoring_yield_dropped`:前回 scored=1204 → 今回 500 で warning、
  前回 scored=0 では判定しない、前回実行なしでも例外にならない
- 中核工程 failed → `failed`、非中核工程 failed → `degraded`
- 火曜の実行:週次 8 工程が skipped でも `succeeded`(所見なしの場合)

`tests/unit/test_monitoring.py`(既存があれば修正、無ければ新規)
- 戻り値が `list[HealthFinding]` になっても**閾値判定の結果が変わらない**
  (E-2 の `sanitized` を成功に数える挙動を含む)
- 既存のログ出力が維持されている(`caplog` で検証)

`tests/unit/test_daily_pipeline.py`(既存があれば追記)
- `run_daily_pipeline()` の戻り値の形が従来どおり
- 非中核工程が例外を投げてもパイプラインが完走する(握り潰しの維持)
- `insider` の失敗が `short_interest` の実行を妨げない(§3.5 ※の分割の検証)

API テスト
- `/pipeline/runs/latest` が記録ゼロ件で 404 ではなく空を返す
- 孤児 run(`finished_at` NULL・6 時間超)が `failed` + `run_orphaned` で返る

---

## 8. 実装順序

1. モデル 2 つ + Alembic(`down_revision = "f9b2d6e1a3c7"`)→ `alembic upgrade head` で通ることを確認
2. `monitoring.py` を `list[HealthFinding]` 返しに変更 + `check_pipeline_health()` 追加 → テスト
3. `pipeline_recorder.py` → テスト
4. `daily_pipeline.py` に組み込み(seq 6/7 の try 分割を含む)→ テスト
5. API 2 本 + スキーマ → テスト
6. `types.ts` / `client.ts` → `PipelinePage.tsx` / `pipelineHealth.ts` / `pipelineStages.ts` → ルート・ナビ・CSS
7. `CollectionStatusBanner` の拡張(§6.5)
8. `uv run pytest` 全通過 + `npm run build` 通過

---

## 9. 実装時に守ること

- **既存の握り潰し構造を「ついでに直さない」。** どの工程が全体を止めるかは
  18.4 の縮退運用として意図的に決められており、コメントで根拠が書かれている。
  本作業は**記録を足すだけ**で、失敗時の制御フローは変えない
  (唯一の例外が §3.5 ※の try 分割で、これは記録の正確さのために必要)。
- **`monitoring.py` の閾値を調整しない。** E-2 のコメントが実データ(sanitized
  約 18.7%)に基づく根拠を残している。新しい判定を足すのはよいが、既存の
  数値は触らない。
- **`skipped` / `succeeded だが成果ゼロ` / `failed` を 1 つの状態にまとめない。**
  この画面の全価値がここにある。
- 日付は UTC 基準(8.4 / 20.3)。表示のみ JST に変換する。
- コメントは既存コードに合わせて日本語、**なぜそうしたか**を書く
  (「何をしているか」はコードが語る)。要件番号での参照(14.15、18.4、18.7 等)
  も既存の流儀に合わせる。
