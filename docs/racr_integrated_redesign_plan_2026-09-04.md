# RACR統合再設計 実装計画

**作成:** 2026-09-04 JST
**基準監査:** `autoscreener_racr_integrated_redesign_audit_2026-09-04.md`(監査時HEAD `5d09cda`)
**計画時HEAD:** `765948c`(clean、`origin/main`と同期済み)

---

## 0. 計画の前提と、監査からの差分

監査は `5d09cda` 時点で書かれている。計画着手時点で以下は**既に解消済み**であり、監査のP0項目から除外する。

| 監査の指摘 | 現状 |
|---|---|
| 「Gitの18 local commitsをremoteへ反映するか判断」 | 解消済み。`main` は `origin/main` と同期、worktree clean |

一方、監査の他のP0指摘はコードで再確認した結果、**すべて現存する**。

| 指摘 | 確認方法 | 結果 |
|---|---|---|
| pipeline全体にouter `try/finally` が無い | `grep -c "finally:" src/autoscreener/batch/daily_pipeline.py` | `0`。core stageが送出すると `recorder.finish()` に到達せず `running` のまま残る |
| テストDBが分離されていない | `tests/conftest.py` の `_ALLOWED_HOSTS` | localhost許可のみ。DB名検証もTEST_DATABASE_URL要求も無い |
| `forward_validation_v5` 未配線 | `src/autoscreener/pipeline_stages.py` の `RESERVED_STAGE_NUMBERS` | 26番は予約のまま、実行列に無い |
| `/ready` がpipeline鮮度を見ない | `src/autoscreener/api/main.py:143` | DB到達性と設定検証のみ |
| default objectiveが `ten_bagger` | `config/objectives.yaml:3` | 変更無し |
| `risk_adjusted` が損失確率を掛けない | `src/autoscreener/scoring/v5/objectives.py` | 変更無し |

**したがって監査の結論「モデル数式より先にP0」をそのまま採用する。**

---

## 1. 全体シーケンスと分割方針

監査のP0–P7をそのまま作業単位にはしない。P2–P4は数ヶ月規模であり、1回の実装セッションに収まらない。以下の Work Package (WP) に割り直す。各WPは**単独でテスト可能・単独でcommit可能・後続をblockしない**大きさにしてある。

| WP | 監査対応 | 内容 | 依存 | 状態 |
|---|---|---|---|---|
| **WP-A** | P0 | 運用安全性:test DB強制、run finalization、orphan sweep、gate FK耐性、V5 forward validation配線、operational readiness | なし | **sonnetへ委譲(2026-09-04)** |
| **WP-B** | P1 | RACR output contract:CE CAGR、P(CAGR>15/20/25%)、terminal loss命名整理、RACR objectiveのshadow追加、Permanent Loss/MDDは明示的 `unavailable` | なし(WP-Aとファイル集合が素) | **sonnetへ委譲(2026-09-04、worktree隔離)** |
| WP-C | P1後半/P5 | RACR contractのAPI schema・frontend型・Ranking/Detail UI反映、default sortとURL state | WP-A, WP-B | 次段 |
| WP-D | P2 | canonical feature/reliability層:registryのtransform/sector normalization/freshnessを実処理へ、`ModelFeatureValue` 永続化 | WP-B | 次段 |
| WP-E | P3 | future state model:margin/cash conversion/share/net-debt/dividend path、reverse valuation、state別provenance修正 | WP-D | 後段 |
| WP-F | P4 | competing-risk + path simulation:Permanent Loss、Expected MDD、recovery time。`unavailable` を初めて外す | WP-E、delisting backfill | 後段 |
| WP-G | P6 | PIT backfill・forward return蓄積・nested walk-forward・calibration | WP-A(forward配線)が前提 | 継続運用 |
| WP-H | P7 | champion–challenger昇格判定 | WP-G | 最終 |

**不変条件(全WP共通):**

1. **V4を壊さない。** `scoring/moic.py`、`config/scoring.yaml` のchampion挙動と既存API契約は維持する。
2. **未実装を0で埋めない。** Permanent Loss / MDD は値ではなく `unavailable` + reason を返す。UIは「— 未推定」と出す。0%とは出さない。
3. **defaultの切替は最後。** RACRはshadowとして追加し、`default_objective` の変更はWP-Hの昇格判定通過後。
4. **policy parameter(λ)をfitしない。** backtestで最適化せず、固定priorとして持ち 0.5x/1x/2x sensitivity を併記する。

---

## 2. WP-A — 運用安全性(P0)

### A-1. テストDBの強制分離 `tests/conftest.py`

現状の `_ALLOWED_HOSTS` はlocalhostを通すだけで、**接続先DB名を一切見ていない**。テストfixtureがTickerを作成・削除するため、日次pipelineと同じDBを壊し得る(監査10.2のFK違反の有力仮説)。

- `TEST_DATABASE_URL` 環境変数を必須にする。未設定ならcollection時点で `pytest.exit()`(fail closed。skipにはしない)。
- DB名が `autoscreener_test` で終わることを検証する。production相当名(`autoscreener`)なら即失敗。
- テストセッション中は `autoscreener.config.get_settings().database_url` を `TEST_DATABASE_URL` へ差し替える。`db/session.py` の `_engine` / `_SessionFactory` のグローバルキャッシュもリセットする。
- テスト実行前に `createdb autoscreener_test` とmigrateが要ることを `docs/` に明記する。

**受け入れ条件:** `TEST_DATABASE_URL` 未設定・production DB名指定の両方で、テストが**開始前に**失敗すること。それを検証するテストを置く。

### A-2. run finalization と orphan回収

- `run_daily_pipeline()` 全体を outer `try/except/finally` で包む。core stageの例外が抜けても finalization が必ず走り、runを `failed` で確定させる。
- `PipelineRecorder` に `heartbeat()` を追加し、stage境界で `pipeline_runs` の更新時刻を進める。
- orphan sweeper:一定時間(既定90分、設定可)heartbeatの無い `running` runを `aborted` へ落とす。pipeline開始時とCLIの両方から呼べるようにする。
- 2026-09-03の停止runは、sweeperの初回実行で自然に `aborted` へ落ちること。手作業のUPDATEを前提にしない。

**受け入れ条件:** core stageが例外を投げるテストで、run statusが `running` のまま残らないこと。

### A-3. gate工程の並行削除耐性 `batch/apply_gates.py`

現状は同一sessionでTicker全件を先読みし、loop中に `UniverseSnapshot` をinsertする。loop中に外部がTickerを消すとFK違反で全体が落ちる。

- upsertを小さいバッチでcommitし、1件のFK違反で日次全体を落とさない。
- insert直前にticker存在を再確認する(またはDB側 `INSERT ... SELECT FROM tickers` で原子的に解決する)。
- 消えたticker_idはstage resultへ `skipped_missing_tickers` として件数と一部IDを記録する。黙って無視しない。

**受け入れ条件:** loop途中でTickerを削除する再現テスト(監査10.2の障害の再現)を追加し、それが緑になること。

### A-4. `forward_validation_v5` をstage 26として配線

`run_forward_validation_v5()` とCLIは実装済み・real-DB testedで、配線だけが未了。

- `RESERVED_STAGE_NUMBERS` から `PIPELINE_STAGE_SEQUENCE` へ移す(26番。既存番号は再採番しない)。
- `daily_pipeline.py` の `model_v5_shadow` の後、`monitoring` の前に実行する。失敗しても止めないnon-core扱い。
- `PIPELINE_STAGE_COUNT` が26になり、`PipelinePage.tsx` の進捗分母が正しくなることを確認する。

### A-5. operational readiness

`/ready` はDB到達性と設定妥当性という既存の意味を維持する(契約を変えない)。**別に** `/operational-readiness` を追加する。

返す内容:最新 `pipeline_runs` のterminal statusと経過時間、`universe_snapshots` / `scores` / `raw_snapshots` / `model_scores` の最新日付と鮮度判定、alembic head一致、主要datasetのcoverage要約。

**受け入れ条件:** pipelineが落ちている・古い状態で `degraded` を返すこと。`/ready` は従来通り200であること(意味を分ける)。

### A-6. CLI

- `run-pipeline --resume`:完了済みstageを再実行しない。stage checkpointは `pipeline_stage_runs` から読む。長時間collectionの後にgateで落ちた際に2時間を捨てないためのもの。
- `sweep-orphan-runs`:A-2のsweeperを手動起動する。

---

## 3. WP-B — RACR output contract(P1)

### B-1. version分離

現状 configは `v5.phase6`、distribution contractは `v5.phase2`、docsはPhase 11 と表記が割れている。

- distribution contract版を `v5.racr1` へ上げる。既存キーは全て残し、旧 `v5.phase2` を読む側を壊さない。
- model version / schema contract version / UI release を別フィールドで持つ。

### B-2. 分布から追加で出す量(すべて同じCDFから導出する)

| 出力 | 定義 | 備考 |
|---|---|---|
| `ce_cagr` | `exp(E[ln W_H]) - 1` | failure atomのMOIC=0で `E[ln W]` が負無限大へ発散する。**回収率分布が入るまでは失敗時の下限をfloor(例:0.01x)で明示的に切り、その事実をcontractへ `ce_cagr_failure_floor` として記録する。** 黙って落とさない |
| `p_cagr_above_15/20/25` | `P(W_H > 1.15^H)` 等 | 7年なら 2.660x / 3.583x / 4.768x。thresholdはhorizonから計算し、定数を埋め込まない |
| `expected_shortfall_10pct_log` | log-CAGRベースのES | 既存のterminal ESと併記する |
| `p_terminal_wealth_below_0_5` | 既存 `p_moic_below_0_5` の**改名(旧キーは残す)** | 「永久損失」ではない。ラベルは「大幅元本毀損確率」 |
| `p_permanent_loss` | — | 常に `None` + `unavailable_reason: "competing_risk_model_not_implemented"` |
| `expected_max_drawdown` / `p_mdd_above_30/50/70` / `recovery_time_median` | — | 常に `None` + `unavailable_reason: "path_simulation_not_implemented"` |

**恒等式テスト必須:** quantile単調性、`p_moic_2x >= p_moic_3x >= p_moic_5x >= p_moic_10x`、`p_moic_below_1_0 + P(W>1) == 1`、CAGR thresholdとMOIC thresholdの相互変換、`median_cagr` と `p50_moic` の整合。

### B-3. RACR objective

```
RACR = CE_CAGR
     - λ_T * TailLoss10
     - λ_D * DDExcess
     - λ_P * P(PermanentLoss)
     - λ_U * ModelUncertainty
```

`config/objectives.yaml` へ `risk_adjusted_compounding` を追加する(`enabled: true`。ただし `default_objective` は `ten_bagger` のまま)。

- λ初期値: `tail_lambda: 0.35`、`drawdown_lambda: 0.10`、`permanent_loss_lambda: 0.20`、`uncertainty_lambda: 0.50`。
- **未実装項の扱いを明示する。** `DDExcess` と `P(PermanentLoss)` が `unavailable` の間、その項は0として計算し、`explanation` へ `"omitted_terms": ["drawdown", "permanent_loss"]` を必ず載せる。**スコアが「リスク控除済み」だと誤読されないようにする**のがこの項の目的である。
- `ModelUncertainty` は当面 CE CAGR の推定標準誤差の代理として `model_confidence` から導く。導出式をコード内コメントで明記する。
- 既存 `risk_adjusted` は `deprecated: true` を付けて残す。削除しない(champion比較に要る)。

**診断出力必須:** RACR と `expected_return` の Spearman、およびTop20重複数を run metrics へ保存する。現行 `risk_adjusted` は Spearman 0.992 / Top20 18件重複で「別objectiveとして機能していない」状態だった。**同じ失敗を検知できないまま再現しないため、この2値をrun metricsへ残すことをB-3の受け入れ条件に含める。**

### B-4. API後方互換

`api/schemas.py` の既存フィールドは削らない。新フィールドはnullable + `unavailable_reason` 付きで追加する。既存のAPI契約テストが緑のままであること。

---

## 4. 委譲状況

- **WP-A / WP-B を 2026-09-04 に sonnet へ委譲した。** WP-Aはmain worktree、WP-Bは隔離worktree(ファイル集合が素なので並行可)。
- WP-C以降は WP-A/B のマージ後に着手する。

---

## 5. 各WP完了時に残す証跡

監査13章の要求に合わせ、WPごとに `docs/` へ以下を残す。

- 実行したテストとその結果(件数まで)
- 変更したcontract versionと、後方互換の確認方法
- 未実装のまま `unavailable` で出している項目の一覧と、それを外す条件
- 昇格判定に影響する数値(RACR vs `expected_return` の順位相関等)
