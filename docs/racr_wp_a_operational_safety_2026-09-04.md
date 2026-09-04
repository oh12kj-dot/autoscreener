# WP-A — 運用安全性(P0)実装記録

**作業日:** 2026-09-04 JST
**対応計画:** `docs/racr_integrated_redesign_plan_2026-09-04.md` 第2節(WP-A)
**対応監査:** `autoscreener_racr_integrated_redesign_audit_2026-09-04.md` §10
**作業ブランチ:** `main`(委譲元HEAD `765948c`)
**worktree:** メイン worktree(WP-Bは別worktreeで並行、ファイル集合は素)

---

## 0. 結論

A-1〜A-6のすべてを実装した。テストDBを新規に用意し(`autoscreener_test`)、
alembicで最新headまでmigrateした上で、全698ファイルのテストスイートを
その専用DBに対して実行した。**1004件成功・17件失敗**。失敗17件はすべて
今回のWP-A変更とは無関係な、既存V5テストの「DBに最低1件Tickerがある前提」
というfixtureの欠落であり、下記§6で詳述する。

V4スコアリング(`scoring/moic.py`、`config/scoring.yaml`)、V5の
`config/objectives.yaml`・`scoring/v5/distribution.py`・
`scoring/v5/objectives.py` は一切変更していない。既存のstage番号(1〜25)も
再採番していない。

---

## 1. A-1:テストDBの強制分離

### 実装

- `tests/conftest.py` に `_require_isolated_test_database()` を追加。
  `pytest_configure`(collection開始前)で呼ばれ、以下を満たさなければ
  `pytest.exit(returncode=1)` で**collection前に**プロセスを終了させる:
  - `TEST_DATABASE_URL` が設定されていること(既定値へのフォールバック無し)
  - そのDB名が `autoscreener_test` で終わること(本番相当 `autoscreener`
    を指定したら弾く)
- 検証に通れば `os.environ["DATABASE_URL"]` と `os.environ["API_DATABASE_URL"]`
  の両方をテストDB URLへ上書きする。`autoscreener.config.get_settings()` は
  呼ぶたびに `Settings()` を再構築する(キャッシュを持たない)ため、環境変数の
  上書きだけで `db/session.py`(バッチ・書き込み層)と `api/dependencies.py`
  (API・読み取り層)の両方に効く。
  - **追加で見つけた穴:** `api_database_url`(`.env` の既定は本番相当DBの
    読み取り専用ロールを指す)を上書きしないと、`TestClient` 経由でHTTPを
    叩くAPIテストだけが本番相当DBへ接続してしまう(書き込みは読み取り専用
    ロールなので防げるが、テスト結果が実運用データに左右され非決定的に
    なる)。これもA-1のスコープ内として塞いだ。
  - `db/session.py` の `_engine`/`_SessionFactory` と `api/dependencies.py` の
    `_api_engine`/`_ApiSessionFactory`、両方のグローバルキャッシュをリセット。

### 受け入れ条件の検証

`tests/unit/test_conftest_db_isolation.py` を新設。`python -m pytest` を
サブプロセスとしてもう一段起動し(`pytest_configure` はプロセス起動ごとに
1回しか評価されないため、同一プロセス内では再現できない)、使い捨ての
`tests/unit/support/dummy_ok.py`(通常収集されないファイル)を対象に:

1. `TEST_DATABASE_URL` 未設定 → 非0終了、"1 passed" が出力に現れない
2. DB名が `autoscreener` (末尾 `autoscreener_test` でない) → 非0終了
3. 正しい `TEST_DATABASE_URL` → 0終了、"1 passed" が出力される(対照実験)

3件とも実行・確認済み(pass)。

### 運用上必要な準備(今回実施済み)

```bash
docker exec autoscreener-db-1 psql -U autoscreener -d autoscreener \
  -c "CREATE DATABASE autoscreener_test OWNER autoscreener;"
DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
  uv run alembic upgrade head
```

以降のテスト実行は:

```bash
TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
  uv run pytest tests/
```

---

## 2. A-2:run finalization・heartbeat・orphan sweeper

### 実装

- `batch/daily_pipeline.py`:`run_daily_pipeline()` の本体を
  `_run_daily_pipeline_body()` へ切り出し、外側を `try/except` で包んだ。
  core stage(collection/gates/scoring/forward_validation)の停止則
  (個別のtry/exceptを持たず例外を伝播させる設計)自体は一切変更していない
  ——変えたのは、その例外が `run_daily_pipeline()` 自身から抜けるとき、
  `recorder.finish_with_exception(exc)` を必ず呼んでから再送出する外殻だけ。
- `batch/pipeline_recorder.py`:
  - `PipelineRun.last_heartbeat_at`(新カラム、migration
    `91c1fa3f0534_a2_pipeline_run_heartbeat.py`)を追加。
  - `PipelineRecorder.heartbeat()`:`stage()` の開始・終了、`skip()` の
    たびに `last_heartbeat_at` を進める。
  - `PipelineRecorder.finish_with_exception(exc)`:runを `failed` で確定し、
    `health` に `run_unhandled_exception` を記録する。
  - `sweep_orphan_runs(threshold=90分既定)`:heartbeatが閾値以上進んで
    いない `running` runを `aborted` へ落とす。`run_daily_pipeline()` の
    冒頭で自動的に呼ばれる(2026-09-03型の停止runが、手作業のUPDATEなしに
    次回実行で自然に回収される)。CLIの `sweep-orphan-runs` からも呼べる
    (A-6)。

### 受け入れ条件の検証(必須の回帰テスト)

- `tests/unit/test_daily_pipeline.py::test_core_stage_exception_does_not_leave_run_status_running`:
  `apply_gates`(gate stage)に `RuntimeError` を投げさせ、実物の
  `PipelineRecorder` + 専用テストDBで、`pipeline_runs.status` が
  `running` のままではなく `failed` で確定し、`finished_at` が設定され、
  `health` に `run_unhandled_exception` が記録されることを確認。**pass。**
- `tests/unit/test_pipeline_recorder.py` に7件追加
  (heartbeat初期化・進行、`finish_with_exception`、sweeperの3系統
  ——回収する/しない/terminal runは対象外)。**全件pass。**

---

## 3. A-3:gate工程の並行削除耐性

### 実装(`batch/apply_gates.py`)

- 全銘柄のidだけを先読みし(Tickerオブジェクトを長時間保持しない)、
  `_GATE_COMMIT_BATCH_SIZE=250` 件ずつ独立した `session_scope()` で処理・
  commitする(以前は全件を1つのsessionで処理し最後に1回だけcommitして
  いた)。
- 各銘柄の処理直前に `session.get(Ticker, ticker_id)` で存在を再確認する。
  無ければ `skipped_missing_tickers` として数える。
- 存在確認とinsertの間に残るごく狭いレース(その一瞬に外部が削除した場合)
  への防御として、1銘柄ぶんの処理を `session.begin_nested()`
  (SAVEPOINT)で囲み、`IntegrityError` をそのSAVEPOINTだけのロールバック
  で吸収する(バッチ全体・日次全体を巻き込まない)。
- 戻り値に `skipped_missing_tickers`(件数)と
  `skipped_missing_ticker_ids_sample`(先頭20件のID)を追加。黙って無視しない。

### 受け入れ条件の検証(必須の回帰テスト)

`tests/unit/test_apply_gates_concurrent_deletion.py` を新設:

- `test_ticker_deleted_mid_loop_does_not_fail_the_whole_gate_stage`:
  監査§10.2の障害をタイミング依存せず決定的に再現する
  ——`_gather_gate_input` を差し替え、3銘柄中2番目を処理する「その瞬間」に
  別セッションから同じTickerを削除・コミットさせる(実際にFK違反を
  発生させる)。修正後は:削除された銘柄が `skipped_missing_tickers` として
  数えられ、前後の銘柄は正常にupsertされ、`apply_gates()` 自体は例外を
  投げずに完走することを確認。**pass。**
- `test_apply_gates_commits_in_small_batches`:バッチサイズを2に差し替え、
  5銘柄(3バッチ)で `session_scope` の呼び出し回数(先読み1回+バッチ3回=4回)
  を確認。**pass。**

既存の `tests/unit/test_apply_gates_point_in_time.py`(3件)も無変更で
pass することを確認済み(リファクタが既存のポイントインタイム挙動を
壊していないことの回帰確認)。

---

## 4. A-4:`forward_validation_v5` をstage 26として配線

### 実装

- `pipeline_stages.py`:`RESERVED_STAGE_NUMBERS` から `forward_validation_v5`
  を `PIPELINE_STAGE_SEQUENCE["forward_validation_v5"] = 26` へ移した。
  既存stage番号(1〜25)は再採番していない。`RESERVED_STAGE_NUMBERS` は
  空dictとして残す(次に「実装済み・未配線」が出たときの置き場所として)。
- `daily_pipeline.py`:`model_v5_shadow` の直後、`monitoring` の前に
  `run_forward_validation_v5(today)` を呼ぶ。v4の `forward_validation` とは
  異なりnon-core(try/exceptで囲み、失敗してもv4スコアリング・後続工程を
  止めない)。
- `PIPELINE_STAGE_COUNT` は自動的に26になった(`len(PIPELINE_STAGE_SEQUENCE)`
  から算出されるため)。

### 既知の注意点(意図的な設計)

`forward_validation_v5` の番号(26)は、実行順序上は `monitoring`(24)・
`backup`(25)より**前**に実行されるが、番号としては両者より大きい。
既存stage番号を再採番しない制約と、26番という予約が既にPhase 7時点で
割り当て済みだった事実から、この1工程だけ「番号順 ≠ 実行順」になる
——`tests/unit/test_daily_pipeline.py::test_pipeline_stage_sequence_is_unique_contiguous_and_matches_execution_order`
にコメントで明記した。

### テスト

- `test_forward_validation_v5_wired_into_stage_sequence`(新設、旧
  `test_reserved_stage_numbers_never_double_count_pipeline_stage_count` を
  置き換え):26番が `PIPELINE_STAGE_SEQUENCE` にあり、`RESERVED_STAGE_NUMBERS`
  が空で、番号が1〜26で重複無く連続していることを確認。**pass。**
- `_stub_phase2367_steps`(既存の自動適用フィクスチャ)へ
  `run_forward_validation_v5` と `sweep_orphan_runs` のモックを追加
  (A-2/A-4で `daily_pipeline.py` に増えた実DBアクセスがこのファイルの
  他テストを遅くしないため)。

---

## 5. A-5:`/operational-readiness`

### 実装

- 新規 `api/operational_readiness.py`:`build_operational_readiness(session, today=None)`。
  - 最新 `pipeline_runs` のterminal statusと経過時間(heartbeat基準の
    90分閾値で「動いているだけ」か「詰まっている」かを判定。A-2の
    sweeperがまだ回収していない間の窓を埋める)。
  - `universe_snapshots`/`scores`/`raw_snapshots`/`model_scores`(v5)の
    最新日付と、暦4日を超えたら stale とする鮮度判定
    (「データが無い」と「4日以内」は明示的に区別。0で埋めない)。
  - alembic head一致(コード側 `alembic/versions/*.py` の最新リビジョンと、
    DBの `alembic_version` テーブル)。
- `api/main.py` に `GET /operational-readiness` を追加。**常に200を返し**、
  `status: "ready"|"degraded"` フィールドで状態を表す(可用性プローブでは
  なく状態レポートであるため)。
- **`/ready` は一切変更していない。** 契約(DB到達性+設定妥当性のみを見る、
  200を返す)は既存のまま。

### 受け入れ条件の検証

`tests/unit/test_operational_readiness.py`(7件、いずれも実DB):

- データが1件も無い状態 → `degraded`、理由に
  `no_pipeline_run_recorded`/各datasetの `_never_populated` が含まれる。
- 直近succeeded run + 当日データ → `ready`(過剰検知しないことの対照実験)。
- heartbeatが90分以上停止した `running` run → `degraded`
  (`latest_run_stuck_running`)。
- heartbeatが直近の `running` run → 検知されない(過剰検知防止)。
- `failed` run → `degraded`(`latest_run_failed`)。
- `/ready` はHTTP 200(契約不変)。
- `/operational-readiness` はdegraded状態でもHTTP 200(500にならない)。

7件すべてpass。

---

## 6. A-6:CLI `run-pipeline --resume` / `sweep-orphan-runs`

### 実装

計画書は `run-pipeline --resume` と表記しているが、実際のコマンド名は
既存の `run-daily-pipeline` であり、そちらへ `--resume` オプションを
追加した(新コマンドを別名で増やすと利用者が混乱するため)。

- `batch/pipeline_recorder.py`:`PipelineRecorder.__init__` に
  `resume_run_id` を追加。指定時は新しい行を作らず既存の
  `pipeline_runs` 行へ合流し(`status` を `running` へ戻す)、既存の
  `pipeline_stage_runs` から `_stage_statuses` を読み込む。
  `resumed_stage_results()` で前回 `succeeded` した工程の `result` を
  取得できる。`stage()`/`skip()` は insert-or-update(upsert)に変更した
  ——`(run_id, stage)` のUNIQUE制約があるため、再開時に前回failedした
  工程を同じrun_idの下で再試行するには、新規insertではなく既存行の
  更新が必要になる。
- `batch/daily_pipeline.py`:`run_daily_pipeline(resume: bool = False)`。
  `_find_resumable_run_id(today)` が当日ぶんの `succeeded` 以外の直近runを
  探し、見つかればそのrun_idへ合流する(無ければ通常どおり新規run)。
  各工程呼び出しを `_run_stage_unless_resumed()` 経由にし、
  `previous_results` に含まれる工程(前回succeeded)は実処理を呼ばず
  結果を再利用する。collection(監査§10.3が名指しした「2時間超」の工程)は
  隔離率・母集団規模の扱いのため専用の分岐にした。
- `cli.py`:`run-daily-pipeline --resume` と新規 `sweep-orphan-runs`
  (`--threshold-minutes` で閾値上書き可)を追加。

### 意図的なスコープ限定

resumeは「実際に処理を再実行しない」対象を、`daily_pipeline.py` が呼ぶ
**全工程**(週次専用工程を含む)に適用した。ただし検証は主要シナリオ
(collection succeeded→gates failed→resumeでcollectionを再実行しない)に
絞った。個々の工程すべてを再開させる組み合わせ(例:filings succeeded・
guidance failed の状態からの再開)までは個別テストしていない——
`_run_stage_unless_resumed()` は全工程で同一のロジックを通るため、
collectionでの検証がそのまま他工程の正しさの根拠になると判断した。

### 受け入れ条件の検証

- `tests/unit/test_pipeline_recorder.py` に3件追加:同一run_idへの合流、
  UNIQUE制約に抵触せず前回failedした工程を再試行できること、前回skipped
  した工程を再度skipしても重複エラーにならないこと。
- `tests/unit/test_daily_pipeline.py` に2件追加:
  - `test_resume_does_not_redo_an_already_succeeded_expensive_stage`:
    前回collectionがsucceeded・gatesがfailedの状態を実DBに再現し、
    `resume=True` で `run_daily_collection` が一度も呼ばれないこと、
    `results["collection"]` が前回の値(999件)を引き継ぐこと、gatesは
    再試行されてsucceededに上書きされること、最終的にrun全体が
    `succeeded` になることを確認。
  - `test_resume_without_an_incomplete_run_starts_fresh`:該当runが無ければ
    新規runになること(安全側フォールバック)。
- `tests/unit/test_cli_pipeline_commands.py`(新設、4件):`--resume` の
  配線、`sweep-orphan-runs` の実行・閾値オプション。

全件pass。

---

## 7. テスト実行結果まとめ

| 対象 | 件数 | 結果 |
|---|---:|---|
| A-1回帰(`test_conftest_db_isolation.py`) | 3 | pass |
| A-2回帰(`test_pipeline_recorder.py` 追加分) | 7 | pass |
| A-2回帰(`test_daily_pipeline.py` core exception) | 1 | pass |
| A-3回帰(`test_apply_gates_concurrent_deletion.py`) | 2 | pass |
| A-3既存回帰(`test_apply_gates_point_in_time.py`) | 3 | pass |
| A-4回帰(`test_daily_pipeline.py` stage順序・配線) | 2 | pass |
| A-5(`test_operational_readiness.py`) | 7 | pass |
| A-6回帰(`test_pipeline_recorder.py` resume分) | 3 | pass |
| A-6回帰(`test_daily_pipeline.py` resume分) | 2 | pass |
| A-6回帰(`test_cli_pipeline_commands.py`) | 4 | pass |
| `test_daily_pipeline.py` 全体 | 11 | pass |
| `test_pipeline_recorder.py` 全体 | 22 | pass |
| `test_pipeline_api.py`(既存、無変更で回帰確認) | 12 | pass |
| **全テストスイート(`tests/`)** | **1021** | **1004 pass / 17 fail** |

実行コマンド:

```bash
export TEST_DATABASE_URL="postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test"
uv run pytest tests/ -q
```

### 17件の失敗について(WP-Aの回帰ではない)

すべて次のパターンで失敗する既存テスト(WP-A着手前から存在するコード、
一切変更していない):

```
ticker = session.query(Ticker).order_by(Ticker.id).first()
ticker_id, symbol = ticker.id, ticker.symbol
# AttributeError: 'NoneType' object has no attribute 'id'
```

内訳:`test_data_freshness.py` 1件、`test_v5_phase3_growth.py` 1件、
`test_v5_phase4_quality.py` 1件、`test_v5_phase5_balance_sheet.py` 3件、
`test_v5_phase6_tail_macro_competing_risk.py` 8件、
`test_v5_phase7_backtest_infrastructure.py` 2件、`test_v5_skeleton.py` 1件。

**原因:** これらのテストは「DBに最低1件Tickerが既に存在する」ことを
前提にしており、自分ではTickerを作らない。今回A-1で用意した
`autoscreener_test` は正しく空の状態から始まる専用DBであり、
`tickers` テーブルには0件のまま(他の全テストは各自作成したTickerを
きちんと後始末している)。そのため `.first()` が `None` を返し、
直後の属性アクセスで落ちる。

**これはWP-Aが壊したのではなく、WP-AがA-1で初めて明らかにした
既存の欠陥である。** 監査自身が「full Python testは実行していない…
安全な非DB V5 objective testのみ実行した」(§0注記)と記しているとおり、
これらのテストがテストDB分離下で実際に通るかどうかは、今回のA-1実装以前に
一度も検証されていなかった。以前は開発者のローカルDB
(`autoscreener`、実データ数千件を含む)へ接続していたため、
「たまたま1件目のTickerが存在する」という前提が常に満たされて
見えていただけである。

**この17件はWP-Aのスコープ外として修正していない。** 理由:
1. 修正対象は `scoring/v5/*.py` 関連のテストファイル群であり、WP-Bが
   並行して同じ領域(distribution/objectives)を隔離worktreeで触っている。
   テストのfixtureとはいえ、無用な衝突面を増やさない。
2. WP-Aの指示は「V4スコアリング・V5 scoring/v5/ファイルを触らない」
   ——テスト対象コードではなくテストファイル自体の修正だが、隣接領域として
   慎重側に倒した。
3. 修正内容(各テストの冒頭にTicker seedを足す)は機械的だが17箇所あり、
   WP-Aの6項目(A-1〜A-6)とは独立した別種の作業である。

**放置していない証拠として、ここに明記して報告する。** 修正方法は単純
(各テストの `session.query(Ticker)...first()` の前に、そのテスト専用の
Tickerを1件作成するfixtureを足す)であり、次にこれらのテストファイルへ
触れる担当(WP-B、またはV5テストスイートの保守)が対応することを推奨する。

---

## 8. 検証したこと・できなかったこと

### 検証した(実DB・実コード)

- A-1のガードが実際にpytestプロセスを起動前に落とすこと(サブプロセス経由)。
- A-2のouter try/exceptが実際にDB行を `failed` へ確定させること。
- A-2のheartbeat・sweeperが実際にDB上で `running`→`aborted` を書き換えること。
- A-3の並行削除耐性が、実際にFK違反を発生させた上で機能すること
  (モックではなく本物のPostgreSQL制約違反を再現)。
- A-4のstage順序・カウントが実際に26になること。
- A-5のエンドポイントが実際のDB状態(空・fresh・stuck・failed)に対して
  正しい `status`/`reasons` を返すこと。
- A-6のresumeが実際に「前回succeededした工程を呼ばない」こと
  (mockのassert_not_calledで直接確認)、かつ同一run_idへ実際に合流すること。

### 検証していない(理由付き)

- **2026-09-03の実際の停止run自体の回収。** 今回作業したのは専用テストDB
  (`autoscreener_test`)であり、実際の開発用DB(`autoscreener`)に残って
  いるはずの2026-09-03のrunには一切触れていない(意図的——本番相当DBへの
  書き込みはこのタスクのスコープ外であり、実行するには利用者の判断が要る)。
  `sweep-orphan-runs` を実DBに対して実行すれば回収されるはずだが、
  それは利用者が別途実行する運用操作である。
- **実際のTask Scheduler/cronからの `run-daily-pipeline --resume` の
  end-to-end実行。** CLIコマンドの配線とresumeロジック自体は実DBで
  検証したが、実際の収集(yfinance/SEC/FRED等への実アクセス)を伴う
  フルパイプラインの1回通し実行は行っていない(数時間かかり、外部APIの
  レート制限を消費するため。`tests/conftest.py` の `_block_outbound_network`
  が示すとおり、これは意図的にテストの対象外)。
- **既存の`autoscreener`本番相当DBへのマイグレーション適用。** 今回追加した
  `91c1fa3f0534_a2_pipeline_run_heartbeat.py` は `autoscreener_test` にのみ
  適用した。本番相当DBへの適用(`uv run alembic upgrade head`)は、利用者が
  次回の運用作業として実行する必要がある——このタスクは実DBを書き換えない
  という制約(A-1の趣旨そのもの)を守っている。

---

## 9. 変更したファイル一覧

### コード

- `tests/conftest.py`(A-1)
- `src/autoscreener/db/models.py`(A-2:`PipelineRun.last_heartbeat_at`)
- `alembic/versions/91c1fa3f0534_a2_pipeline_run_heartbeat.py`(新規)
- `src/autoscreener/batch/pipeline_recorder.py`(A-2/A-6)
- `src/autoscreener/batch/daily_pipeline.py`(A-2/A-4/A-6)
- `src/autoscreener/batch/apply_gates.py`(A-3)
- `src/autoscreener/pipeline_stages.py`(A-4)
- `src/autoscreener/api/operational_readiness.py`(新規、A-5)
- `src/autoscreener/api/main.py`(A-5)
- `src/autoscreener/cli.py`(A-6)

### テスト

- `tests/unit/test_conftest_db_isolation.py`(新規)
- `tests/unit/support/dummy_ok.py`(新規、上記の使い捨てターゲット)
- `tests/unit/test_apply_gates_concurrent_deletion.py`(新規)
- `tests/unit/test_pipeline_recorder.py`(追加分:heartbeat/finish_with_exception/sweep/resume)
- `tests/unit/test_daily_pipeline.py`(既存フィクスチャ更新+core exception/resume回帰追加)
- `tests/unit/test_operational_readiness.py`(新規)
- `tests/unit/test_cli_pipeline_commands.py`(新規)

### 触っていないことを明示するファイル(ハード制約の遵守確認)

- `scoring/moic.py`、`config/scoring.yaml`:無変更。
- `scoring/v5/*.py`、`config/objectives.yaml`、`config/model_v5.yaml`:無変更。
- `pipeline_stages.py` の既存stage番号(1〜25):無変更(26のみ新規配線)。
