# Live Investment Intelligence「未取得」是正・完全実装計画（2026-09-01）

**対象リポジトリ:** `C:\AI\App_Dev\AutoScreener`
**基準ブランチ:** `main`
**基準日:** 2026-09-01
**対象:** 銘柄詳細の Live Investment Intelligence 全セクション、関連API、収集・計算バッチ、日次ジョブ、DB、coverage画面
**想定読者:** 本修正を実装する別AIモデル、Codex、開発者、運用担当者

---

## 0. 引継ぎ先AIへの実行指示

この文書は提案集ではなく、2026-09-01のコード・本番相当DB監査を根拠にした実装仕様である。
以下を完了条件として扱うこと。

1. 本文書の **Phase 0～8をすべて実装**する。UIだけ、collectorだけ、テストだけで完了扱いにしない。
2. `P(horizon年でtarget_moic倍)`、既存ゲート、ランキング順を変更しない。Live Intelligenceは引き続き `Not used in ranking` とする。
3. 欠損を0へ変換しない。`未試行`、`取得済み・該当なし`、`取得成功`、`取得失敗`、`対象外`を混同しない。
4. 全データで `observed_at`、原典、coverage、Point-in-Time境界を保持する。
5. SEC/FRED/LLMの外部取得は、テストでは必ずモックする。実ネットワークを通常テストへ持ち込まない。
6. 本番DBを更新するbackfill・収集実行は、コード実装とテスト完了後に件数見積りを示し、明示的な運用工程として実行する。
7. 既存のユーザー記録 `research/<TICKER>.md` と `config/positions.yaml` はGit管理の正本とし、アプリから暗黙に上書きしない。
8. 既存のdirty worktreeがあれば推測で消さない。今回の変更と衝突しない形で保存する。
9. 各Phase終了時に受け入れ条件を確認し、最後に全テスト、frontend build、migration往復、API smoke、実DBcoverageを検証する。
10. passing testsだけを「データが入った」証拠にしない。実DBの対象数、成功、該当なし、失敗、未試行を別々に確認する。

---

## 1. 監査結論と現在値

### 1.1 結論

詳細画面の「未取得」は単一原因ではない。

1. **保存先/API/UIだけ実装され、writerが存在しない:** TAM、マクロ感応度。
2. **自動処理はあるが対象母集団が上位300銘柄に限定:** 資本配分、KPI、債務、経営陣インセンティブ。
3. **収集対象でも前提ソースや計算入力が不足:** SEC本文なし、年次財務2点未満、対応フォーム外、抽出パターン不一致。
4. **ユーザー入力が正本:** テーゼのマイルストーン。
5. **運用未反映または分類器不足:** M&A competing risk。
6. **UIがエラーや未知のcoverageを「未取得」へ潰す:** API 404/500/通信失敗、`collection_failed`、`not_applicable`。

### 1.2 2026-09-01 DB実測ベースライン

実装後の比較用に、次を基準値として保存する。

| データセット | 行数 | 対象銘柄 | coverage補足 |
|---|---:|---:|---|
| `tickers` | 5,893 | 5,893 | benchmarkを含む |
| `raw_snapshots` | 27,486 | 5,794 | 複利・会計品質の入力 |
| `scores` | 7,060 | 1,268 | 逆算評価の入力 |
| `filings` | 189,033 | 344 | メタデータ |
| `filing_sections` | 2,605 | 300 | 10-K/10-Q/8-K/DEF 14A本文 |
| `capital_allocation_events` | 1,604 | 280 | 20銘柄はスキャン済み・該当なし |
| `operating_kpi_observations` | 203 | 73 | 227銘柄はスキャン済み・該当なし |
| `debt_instruments` | 312 | 115 | 185銘柄はスキャン済み・該当なし |
| `liquidity_facilities` | 0 | 0 | writerなし |
| `management_incentive_snapshots` | 282 | 282 | 18銘柄はスキャン済み・該当なし |
| `market_opportunity_estimates` | 0 | 0 | writerなし |
| `market_opportunity_components` | 0 | 0 | writerなし |
| `thesis_milestones` | 0 | 0 | 実ノートなし |
| `macro_series` | 22,854 | - | DGS10/DFII10/BAMLH0A0HYM2のみ |
| `macro_exposure_snapshots` | 0 | 0 | 計算関数はあるがwriterなし |
| `delisting_events` | 0 | 0 | `tickers.delisted_at` は592銘柄 |
| `live_dataset_coverage` | 1,200 | 300 | 4 dataset × 300銘柄 |

直近のscheduled pipelineは `investment_intelligence` がKPI一意制約違反でfailedとなり、run全体は
`degraded` だった。その重複不具合自体は `8721a37` で修正済みで、手動再抽出後に上表の
coverageが入ったが、**修正後の完全なscheduled rerunは未検証**である。

### 1.3 主要な現行接続点

- UI一覧と状態表示: `frontend/src/components/InvestmentIntelligenceSections.tsx`
- API client/type: `frontend/src/api/client.ts`, `frontend/src/api/types.ts`
- API response/query: `src/autoscreener/api/routes.py`, `src/autoscreener/api/schemas.py`
- SEC派生writer: `src/autoscreener/batch/collect_investment_intelligence.py`
- 正規表現extractor: `src/autoscreener/screening/investment_intelligence_extract.py`
- 計算関数: `src/autoscreener/scoring/investment_intelligence.py`
- SEC対象選定: `src/autoscreener/batch/collect_filings.py`
- SEC本文保存: `src/autoscreener/batch/collect_filing_sections.py`
- マクロ生系列: `src/autoscreener/batch/collect_macro.py`
- 日次組込み: `src/autoscreener/batch/daily_pipeline.py`
- DB model: `src/autoscreener/db/models.py`
- CLI: `src/autoscreener/cli.py`
- coverage画面: `frontend/src/pages/DataCoveragePage.tsx`

---

## 2. 完了時の状態契約

### 2.1 coverage status

Python側に単一の `CoverageStatus`（`StrEnum` または `Literal` の正本）を定義し、API schema、collector、
coverage集計、テストが同じ定義を使う。

| 値 | 意味 | UI表示 | coverage成功扱い |
|---|---|---|---|
| `not_collected` | 対象外、未試行、必要ソース未整備 | 理由別。「未取得」「収集対象外」「対応ソース未整備」 | いいえ |
| `collected_no_finding` | 原典を正常にスキャンしたが該当事実なし | 取得済み・該当なし | はい |
| `collected_with_data` | 検証可能なデータあり | データ表示 | はい |
| `collection_failed` | 試行したが失敗 | 取得失敗（再試行可否を表示） | いいえ |
| `not_applicable` | その銘柄/データセットには適用不能 | 対象外 | 成功率の分母外 |

`not_collected`だけでは理由が不足するため、`LiveDatasetCoverage` とAPI responseへ次を追加する。

- `reason_code: str | None`
- `reason_detail: str | None`（機密や巨大traceを入れない）
- `attempted_at: datetime | None`
- `source_scope: str | None`（例: `10-k,item7;10-q,item7;def14a`）
- `retryable: bool | None`

最小の `reason_code` セット:

- `outside_collection_scope`
- `no_raw_snapshot`
- `insufficient_annual_history`
- `no_supported_filing`
- `source_not_scanned`
- `source_scanned_no_match`
- `missing_required_fields`
- `user_input_missing`
- `insufficient_price_history`
- `insufficient_factor_history`
- `provider_error`
- `parse_error`
- `database_error`
- `unsupported_model_family`

### 2.2 API response

`InvestmentIntelligenceResponse` に以下を追加し、`coverage_status: str` を禁止する。

```python
class InvestmentIntelligenceResponse(BaseModel):
    ticker: str
    as_of: datetime.date
    coverage_status: CoverageStatus
    reason_code: str | None = None
    reason_detail: str | None = None
    observed_at: datetime.datetime | None = None
    source: str | None = None
    source_url: str | None = None
    data_age_days: int | None = None
    retryable: bool | None = None
    not_used_in_ranking: bool = True
    data: dict | list | None = None
```

要件:

- HTTP成功とデータcoverageを分離する。正常な欠損はHTTP 200でcoverageを返す。
- 不正tickerは404のままにし、frontendが「APIエラー」と表示する。
- 予期しない500、ネットワーク失敗を `not_collected` に変換しない。
- `as_of` より後の `observed_at` を返さない。
- 行がなくても、最新の `LiveDatasetCoverage` を参照して試行結果を返す。

### 2.3 frontend request state

各セクションを次のdiscriminated unionで保持する。

```ts
type SectionState =
  | { state: "loading" }
  | { state: "loaded"; response: InvestmentIntelligenceResponse }
  | { state: "error"; message: string; status?: number };
```

`Promise.allSettled` のrejected結果を捨てない。UIはcoverage statusをexhaustive switchで描画し、未知値は
警告として表示する。APIエラーと未取得を同じ文言にしない。

---

## 3. Phase 0 — 真実を表示する基盤（P0）

### 3.1 DB migration

新Alembic revisionを作成する。

1. `live_dataset_coverage` に§2.1の列を追加。
2. `coverage_status` に既存5値だけを許すCheckConstraintを追加する。
3. snapshot/event各テーブルの `coverage_status` にも同じconstraintを追加するか、少なくとも書込時の型を共通化する。
4. 既存行は値を変えず、`attempted_at = observed_at`、既知の成功行は `reason_code = NULL` でbackfillする。
5. downgradeで追加列/constraintだけを安全に戻す。

### 3.2 backend共通化

- `src/autoscreener/coverage.py` を追加し、enum、reason code、最新coverage取得、状態集約を置く。
- `_dataset_status()` をstatus文字列だけでなくcoverage row全体を返す関数へ置換する。
- `_intelligence_response()` は、明示coverage、dataset ledger、データ行の順序を明確にして状態を決定する。
- `rows` が空でも `data` が有効なら自動的に `not_collected` へ落とさない。M&Aのような集計系は専用statusを渡す。
- collectorで文字列リテラルを散在させずenumを使う。

### 3.3 frontend表示

- `DataBlock` を状態表示とデータrendererに分離する。
- `collection_failed` は赤/橙の警告と理由・再試行可否を表示。
- `not_applicable` は灰色の「対象外」。
- `not_collected + user_input_missing` は「ユーザー未設定」。
- `not_collected + outside_collection_scope` は「現在の収集対象外」。
- `not_collected + no_supported_filing` は「対応する提出書類が未取得」。
- APIエラーは「取得APIエラー」。再読込ボタンまたはページ再読込の案内を出す。
- source、as-of、observed-at、ageを状態に関係なく表示できるようにする。

### 3.4 テスト

- backend: 5 statusすべてのschema validation/API response。
- backend: 不正なstatusをPydanticとDBが拒否する。
- backend: `as_of` PIT境界。
- frontend: Vitest + Testing Libraryを導入し、loading/error/5 status/未知statusを検証。
- frontend: 500を「未取得」と表示しない回帰テスト。

### 3.5 Phase 0受け入れ条件

- 同じ空データでも、未試行、該当なし、失敗、対象外が別表示になる。
- API通信失敗を「未取得」と表示しない。
- `/data-coverage` が各status件数を失わない。
- 既存API consumerを壊さずfrontend buildが通る。

---

## 4. Phase 1 — 収集対象・失敗記録・トランザクション境界（P0）

### 4.1 対象母集団を1回だけ決める

現在は各jobが独立に `select_tracked_tickers()` を呼ぶため、工程間で対象がずれ得る。pipeline開始時または
SEC工程開始時に対象symbolを確定し、次へ同じリストを渡す。

- `collect_filings(symbols=targets)`
- `collect_filing_sections(symbols=targets)`
- `collect_guidance(symbols=targets)`
- `collect_concentration(symbols=targets)`
- `collect_dilution(symbols=targets)`
- `collect_litigation(symbols=targets)`
- `collect_investment_intelligence(symbols=targets)`

対象は保有、実研究ノート、直近ランキング上位の和集合とし、上限はconfig化したままにする。API詳細を開いた
だけで自動的に外部収集を開始しない。

### 4.2 coverageを「本文があった銘柄」だけに限定しない

現行の `processed_ticker_ids = {section.ticker_id ...}` を廃止する。対象ticker全件についてdataset別に必ず1件の
試行結果を記録する。

- sectionを正常にスキャンし一致あり → `collected_with_data`
- sectionを正常にスキャンし一致なし → `collected_no_finding`
- 対応フォーム自体なし → `not_collected/no_supported_filing`
- 対象外 → ledgerを無理に全5,893銘柄へ作らず、APIで `outside_collection_scope` を返す
- 取得/parse失敗 → `collection_failed`

### 4.3 1銘柄の失敗で全体をrollbackしない

`collect_investment_intelligence()` の全section一括transactionを、銘柄単位のsavepoint/transactionへ変更する。

- 銘柄単位で例外捕捉。
- 成功銘柄はcommit可能にする。
- 失敗銘柄は4 datasetすべてを機械的にfailedにせず、失敗したdatasetだけ記録。
- pipeline stage resultへ `targets/succeeded/no_finding/failed/outside_scope/rows_written` を含める。
- 例外全文はpipeline stage側、銘柄coverageには短いreasonだけを保存する。

### 4.4 SECフォーム対応

`TRACKED_FORMS` と本文抽出に最低限以下を追加する。

- `20-F`: Item 4/5/8相当の事業・財務・リスク情報
- `6-K`: 決算発表添付
- 必要に応じて `40-F`
- 既存 `DEF 14A`, `10-K`, `10-Q`, `8-K` は維持

外国発行体を「該当なし」と誤認しない。フォーム別section mappingを純粋関数としてテストする。

### 4.5 Phase 1受け入れ条件

- 追跡対象300銘柄について、4 SEC派生datasetのstatus合計が各300になる。
- 1銘柄の重複/parse失敗でも他299銘柄のデータがcommitされる。
- 20-F/6-K銘柄で `no_supported_filing` が減り、原典が保存される。
- pipeline resultとDBcoverage件数が一致する。

---

## 5. Phase 2 — 資本配分・KPI・債務・経営陣の完全化（P1）

### 5.1 資本配分

#### 現行欠陥

- 4語だけ、各sectionの最初の一致だけ。
- `equity_raise`, `debt_raise`, `divestiture` を生成しない。
- APIの `three_year_totals` は3年filterをしていない。
- API dataが `{three_year_totals, events}` のネスト構造なのに汎用UIが配列/オブジェクトを捨て、空表になり得る。
- 同一accession・event_typeの複数案件を1件へ潰し得る。

#### 実装

1. extractorを `finditer` ベースにし、同一section内の複数イベントを保持。
2. event typeを既存schemaに合わせて拡張:
   `acquisition/divestiture/buyback/equity_raise/debt_raise/capex/dividend`。
3. S-3/424B5/424B4の増資、10-K/10-Qのbuyback/capex/M&A、8-Kの案件をフォーム別に抽出。
4. `source_excerpt` 相当をイベントに保持できるようmodel/migrationを追加。raw payloadだけに隠さない。
5. dedupe keyを `ticker + accession + event_type + normalized amount + excerpt hash` とする。
6. `three_year_totals` は `announced_at >= as_of - 3 years` で計算。
7. 3年集計にmarket cap比を出す場合は、同じas-ofのPIT時価総額があるときだけ算出。欠損はNone。
8. frontendに専用 `CapitalAllocationSection` を作り、集計とイベント表を表示。

#### テスト

- 同一sectionに複数買戻し/買収。
- 通貨/scale（thousand/million/billion）。
- 増資と単なる「shares」の誤検知防止。
- 3年境界。
- idempotent rerun。
- nested responseが実際に画面表示される。

### 5.2 Operating KPI

1. 既存ARR/NRR/backlog/customer countを維持。
2. model family別のregistry設定を追加し、少なくとも次の候補を構造化:
   SaaS（ARR/NRR/RPO/customers）、marketplace（GMV/TPV/take rate）、consumer（stores/units/ARPU）、
   industrial（backlog/book-to-bill）、mining/energy（production/unit cost/reserves）。
3. 企業定義、期間、単位、報告日、原典excerptを必須化。
4. 同名KPIでも会社定義が変わった場合に履歴を上書きしない。
5. regex一致を「業績予想」「競合他社」「市場全体」の数字と区別する保守的フィルタを置く。

### 5.3 債務・流動性

1. 文中regexに加えてdebt maturity table parserを追加。
2. `LiquidityFacility` writerを実装し、cash、revolver total/drawn/available、ATM/shelf remainingを保存。
3. XBRL/RawSnapshotの現金は補助入力に使えるが、SEC本文と異なる場合はsource conflictとして両方保持。
4. `debt_due_12m = 0` は満期表をスキャンできた場合だけ0。未スキャンはNone。
5. financing review判定はcash/revolverの両方のcoverageを示す。

### 5.4 経営陣インセンティブ

1. DEF 14Aの役員別行を解析し、executive、role、ownership、total compensation、equity compensation、performance metricsを保存。
2. founder/tenureは一次情報がある場合のみ。推測で埋めない。
3. aggregate disclosureは役員別データと混同しない。
4. 外国発行体は20-Fのownership/directors disclosureを別extractorで扱う。
5. ownershipが全株主表の別人物を拾わないfixtureを追加。

### 5.5 Phase 2受け入れ条件

- 資本配分 `collected_with_data` で空表にならない。
- 各値にsource URL/accession/excerptがある。
- debtが未取得なのに `debt_due_12m=0` を返さない。
- 同一入力の再実行で行数が増えない。
- 誤抽出fixtureと正常fixtureが両方通る。

---

## 6. Phase 3 — TAM / Market Opportunity writer（P1）

### 6.1 方針

TAMをLLMに自由生成させない。会社発表値も自動的に真実とみなさない。既存テーブルを使い、
`company_reported`、`bottom_up`、`third_party`、`manual` を分離する。ランキングには入れない。

### 6.2 入力経路

#### A. Git管理の手動/承認済み入力

`research/<TICKER>.md` front matterに後方互換な `market_opportunity` 配列を追加できるようにする。

必須項目:

- `as_of`
- `method`
- `tam_value` または構成要素
- `currency`
- `formula_text`
- `source_url`
- `source_excerpt`
- `confidence`
- `created_by: manual | llm-assisted`

`collect-market-opportunity` はノートを読み、DBへappend-only snapshotとして保存する。ノートはアプリから書き換えない。

#### B. SEC/IR候補抽出

保存済みfiling sectionから `total addressable market`、`serviceable addressable market` 等の明示値だけを
候補抽出する。自動保存時は `method=company_reported`, `created_by=machine`, `confidence=low/medium` とし、
引用とURLがない候補は破棄する。

LLMを使う場合は既存接続設定を利用し、候補JSONを厳格schema検証する。LLM出力は必ず
`created_by=llm-assisted`、`AI extracted — verify source` 表示とする。

### 6.3 派生値

- `penetration_rate = current_revenue_addressable / TAM` は両値が同通貨・同範囲のときだけ。
- TENX 7年後売上との比較は計算時点を揃え、TAM超過は `assumption_conflict` と表示。
- 自動的なTAM成長率は仮定しない。ユーザーが明示した場合はraw payloadとformulaへ残す。

### 6.4 API/UI

- 読取APIは複数estimateをsource/method別に表示。
- component表を専用rendererで表示。
- manual/company/third-party/AI-assisted badgeを表示。
- 値がなければ、スキャン済みかユーザー未設定かをcoverageで区別。

### 6.5 受け入れ条件

- fixture SEC本文から引用付きcompany-reported TAMを保存できる。
- 実研究ノートからbottom-up componentsをidempotentに保存できる。
- 引用/URLなしのLLM候補は保存されない。
- TAM=0やpenetration=0を欠損の代用にしない。
- 現在0件のテーブルが、対象銘柄ではwith_data/no_finding/failureのいずれかへ移る。

---

## 7. Phase 4 — マクロ感応度writer（P1）

### 7.1 生系列設定

`config/collection.yaml` が `FredConfig` のdefaultを上書きしてDEXJPUSを落としているため、少なくとも次を明示する。

- `DGS10`: 米10年金利
- `DFII10`: 米10年実質金利
- `BAMLH0A0HYM2`: HY OAS
- `DEXJPUS`: USD/JPY

Oil/Copper等を追加する場合はseries ID、頻度、変換方法をconfigで宣言し、コードへ直書きしない。

### 7.2 計算仕様

`collect-macro-exposure` を追加する。

1. 対象銘柄のPIT価格履歴とmacro seriesを週次へ整列。
2. 株価はsplit/total-return方針を既存価格データ仕様と一致させる。
3. 金利・spreadは週次差分、価格/為替/commodityは週次returnなど、factor別transformをconfig化。
4. 最低52週、推奨104週。不足時は `not_collected/insufficient_*_history`。
5. 純粋関数 `macro_exposure()` を使い、通常betaとdownside betaを計算。
6. `observation_end`、sample count、factor transform、window、source series IDをraw payloadへ保存。
7. 計算結果がNoneでもスキャン済みなら理由を記録し、0へしない。
8. 週次または月次pipeline stageとして独立記録する。

### 7.3 PIT・統計上の注意

- `observed_at` より後のmacro observation/priceを使わない。
- FRED改定系列は現DBがvintageを保持しない。vintage未対応の系列はその制約をAPI/UIに表示する。
- betaは相関であり因果ではない注記を維持。
- sample数、window、stalenessをUIに表示。

### 7.4 受け入れ条件

- 十分なfixtureで既知betaを許容誤差内に復元。
- 52週未満、factor variance=0、欠損日、非有限値を正しく分類。
- 同じobserved_at/factorの再実行が重複しない。
- 対象銘柄ごとに各factorのcoverageが取得可能。
- `macro_series` があるだけで「マクロ感応度取得済み」と誤表示しない。

---

## 8. Phase 5 — マイルストーン、M&A、計算系の欠損理由（P1）

### 8.1 テーゼのマイルストーン

- `research/TEMPLATE.md` の形式を正本とする。
- 実ノートなしは `not_collected/user_input_missing`。
- ノートあり・milestones空は、明示的に空を指定した場合のみ `collected_no_finding`。
- YAML不正は `collection_failed/parse_error` とし、既存の正常snapshotを削除しない。
- UIにノート作成手順とファイルパスを出すが、アプリからGit管理ファイルを自動更新しない。

### 8.2 M&A competing risk

#### 移行

1. `tickers.delisted_at` がある592銘柄を `delisting_events` へbackfillする。
2. 根拠が提出日だけなら `event_type=unknown`、推測でacquisition/bankruptcyへ分類しない。
3. source/source URL/observed_atを保持。

#### 将来分類

- 8-K、DEFM14A、SC TO、破産/清算一次資料等から分類候補を作る。
- cash/stock considerationが確認できた場合だけsettlementを保存。
- source conflictはunknownへ戻さず両根拠を保持し、人間確認状態を出す。

#### API修正

M&A endpointのデータを分離する。

- `population_statistics`: 全delisting event母集団の集計とcoverage。
- `ticker_events`: 個別銘柄のイベント。空でも母集団統計のstatusを `not_collected` にしない。
- 母集団0件なら `not_collected/source_not_scanned`。

### 8.3 複利の質・利益の質・逆算評価

- `RawSnapshot` なし → `no_raw_snapshot`。
- 年次履歴2点未満 → `insufficient_annual_history`。
- 必須入力のどれが欠けたか `missing_fields` をdataまたはreason detailへ返す。
- Accounting Qualityは入力の大半がNoneでも無条件 `collected_with_data` にしない。算出可能指標と不能指標を分ける。
- Reverse ValuationはScoreなし、`score.inputs` なし、unsupported model familyを別理由で返す。

### 8.4 JPYシナリオ

- `config/collection.yaml` にDEXJPUSを追加。
- FRED/yfinanceのsourceとas-ofをJPYシナリオにも伝播。
- `expected_moic` なしとFXなしを別理由で表示。
- yfinance fallback失敗を単なる空配列にしない。

---

## 9. Phase 6 — coverage画面と運用監視（P1）

### 9.1 denominatorをdataset別にする

現在の `/data-coverage` は全非benchmark銘柄を分母にする一方、SEC収集は300銘柄上限であり、正常でも約5%にしかならない。
次の項目を返す。

- `universe_count`
- `eligible_count`
- `targeted_count`
- `attempted_count`
- `with_data_count`
- `no_finding_count`
- `failed_count`
- `not_applicable_count`
- `not_collected_count`
- `stale_count`
- `last_successful`
- `last_attempted`
- `source`

coverage率は少なくとも次の2種類を分ける。

- `operational_coverage = successful / targeted`
- `universe_coverage = with_data / eligible`

### 9.2 pipeline health

- Investment Intelligence stageの`failed > 0`はrun health findingにする。
- stage successでも`targets > 0 && attempted = 0`をsilent failureとして警告。
- TAM/macro stageも独立表示。
- scheduled rerun成功前はREADMEの「完全再実行未検証」を消さない。

### 9.3 UI

- coverage画面にstatus別件数と分母定義を表示。
- 5%を「失敗」と誤読しないよう、対象上限300/全母集団を併記。
- datasetクリックで対象外理由・失敗理由の上位集計を表示できる形を検討する。

---

## 10. Phase 7 — backfill・本番収集・検証（P0運用）

### 10.1 実行前

1. DB backupを作成し、restore testを通す。
2. `alembic current` と新headを確認。
3. `.env` は値を表示せず、`EDGAR_USER_AGENT`、`FRED_API_KEY` のSET/ABSENTだけ確認。
4. 対象銘柄数、想定SECリクエスト数、FRED系列数、所要時間を出す。
5. rate limit/circuit breaker設定を確認。

### 10.2 推奨実行順

```text
alembic upgrade head
refresh-cik-map
collect-filings -- tracked targets
collect-filing-sections -- same targets
collect-guidance / concentration / dilution / litigation
collect-investment-intelligence -- same targets
collect-market-opportunity -- same targets
collect-macro
collect-macro-exposure -- same targets
collect-delistings / delisting-event backfill
run-daily-pipeline -- scheduled相当の完全再実行
```

実際のCLI名はPhase実装で確定し、READMEへコピー可能な形で記録する。

### 10.3 実行後SQL/確認

- 各datasetの行数・distinct ticker数。
- `live_dataset_coverage` のdataset/status/reason別集計。
- target 300銘柄のstatus合計が300になること。
- duplicate keyがないこと。
- source URL/excerpt欠損率。
- stale率。
- pipeline latest run/status/stage result。
- 代表銘柄: with data、no finding、failed fixture/実例、対象外、外国発行体。

本番収集が失敗した場合、コード成功と運用成功を分けて報告し、空テーブルのまま完了にしない。

---

## 11. Phase 8 — テスト、文書、最終受け入れ（P0）

### 11.1 backend tests

追加/拡張対象:

- `tests/unit/test_collect_investment_intelligence.py`
- `tests/unit/test_investment_intelligence_extract.py`（新規）
- `tests/unit/test_market_opportunity.py`（新規）
- `tests/unit/test_macro_exposure_collection.py`（新規）
- `tests/unit/test_api_investment_intelligence.py`（新規）
- `tests/unit/test_data_coverage.py`（新規または既存API testへ追加）
- `tests/unit/test_daily_pipeline.py`
- migration upgrade/downgrade smoke

必須ケース:

- 5 coverage status + reason。
- idempotency。
- 同一日複数section/同一KPI。
- 1銘柄失敗時の部分commit。
- 20-F/6-K。
- PIT cutoff。
- stale。
- manual/AI/company source区別。
- nested payload。
- missing data ≠ 0。
- global M&A statistics + empty ticker events。

### 11.2 frontend tests

Vitest/Testing Libraryを導入し、少なくとも次をテストする。

- 各statusラベル。
- HTTP error表示。
- Capital Allocation nested data。
- TAM components。
- Macro exposure sample count/因果注記。
- coverage denominator表示。
- source/as-of/age。

### 11.3 文書

- ルートREADMEのLive Intelligence初回・定期収集順。
- datasetごとの「なぜ必要か／どう使うか／どこで取得するか」。
- `docs/README.md` の本計画リンク。
- `.env.example` とconfig例。
- 失敗時の運用runbook。
- 実測coverageとscheduled rerun結果。

### 11.4 最終コマンド

WindowsではPowerShellの実行制約を考慮し、次を使う。

```text
uv --cache-dir .uv-cache run pytest -q
npm.cmd run lint
npm.cmd run build
uv --cache-dir .uv-cache run alembic downgrade <previous-head>
uv --cache-dir .uv-cache run alembic upgrade head
git diff --check
```

DB migration往復は使い捨て/検証DBで行い、本番DBをdowngradeしない。

### 11.5 最終受け入れ条件

以下を全て満たすまで完了ではない。

- [ ] TAMとMacro Exposureに実writer、CLI、pipeline、API、UI、testsがある。
- [ ] 対象銘柄のTAM/Macroがwith_data/no_finding/failureのいずれかになり、全件0ではない。
- [ ] API/network errorが「未取得」と表示されない。
- [ ] `collection_failed` と `not_applicable` が正しく表示される。
- [ ] SEC対象300銘柄の試行状態がdatasetごとに300件揃う。
- [ ] 外国発行体の20-F/6-K経路がある。
- [ ] 資本配分データが空表にならず、3年集計が本当に3年に限定される。
- [ ] debt未取得を0として表示しない。
- [ ] マイルストーン未設定を取得失敗と表示しない。
- [ ] `delisting_events` が0件ではなく、根拠不十分な分類はunknownのまま保持される。
- [ ] M&A母集団統計が、個別イベントなしの現役銘柄でも表示される。
- [ ] `/data-coverage` が対象分母と全体分母を分ける。
- [ ] scheduled相当の完全pipelineが成功またはdegraded理由を正確に記録する。
- [ ] full pytest、frontend lint/build、migration検証、API smokeが通る。
- [ ] 実DB件数とcoverageが文書化される。
- [ ] Core probability、ゲート、ランキングが変わっていないことをsource scanと回帰テストで確認する。

---

## 12. 推奨PR/コミット順

大きな一括変更を避け、次の順序で完了可能な単位に切る。

1. **LI-001 Coverage contract and API truthfulness**
   enum、reason、migration、API schema、UI error/status、tests。
2. **LI-002 Collection scope and per-ticker isolation**
   共通target、全target coverage、savepoint、pipeline result、20-F/6-K基盤。
3. **LI-003 Capital allocation renderer and extractor**
   multi-match、event type、dedupe、3年filter、専用UI。
4. **LI-004 KPI, debt and incentives coverage**
   registry拡張、debt table、liquidity writer、DEF 14A/20-F。
5. **LI-005 Market opportunity writer**
   research note schema、SEC候補抽出、CLI、API/UI、tests。
6. **LI-006 Macro exposure writer**
   factor config、PIT alignment、batch、pipeline、API/UI、tests。
7. **LI-007 Milestones, M&A and computed-data reasons**
   user未設定、delisting backfill、M&A status、raw/score不足理由、JPY。
8. **LI-008 Coverage operations and acceptance run**
   coverage画面、pipeline health、backfill、完全rerun、docs、final verification。

各コミットで`git diff --check`と関連テストを通し、LI-008で全suiteを実行する。途中コミットでもDB/APIの
後方互換を壊さない。migrationを含むコミットを後続コードから分離する場合は、旧コードが追加nullable列を無視できる状態にする。

---

## 13. 明示的にやらないこと

- Live IntelligenceをCore score/gateへ投入する。
- TAM、M&A分類、founder、報酬KPIを根拠なしにLLMで補完する。
- 欠損を0、false、空配列へ一律変換する。
- API詳細表示のたびに高コスト/外部収集を起動する。
- 全5,893銘柄のSECを毎日無制限にクロールする。
- FRED vintage未保存の系列を完全なPoint-in-Time系列だと主張する。
- `research/` のユーザー記録をDB/UIから暗黙に上書きする。
- passing unit testsだけをデータ投入完了の証拠にする。
