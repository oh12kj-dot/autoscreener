# 日次パイプライン取得データ完全性・改善引き継ぎ（2026-09-04）

## 0. この文書の目的

この文書は、日次パイプラインの高速化後に残っているデータ不足と実装上の欠陥を、
別のAIまたは担当者が推測せず修正できるように引き継ぐためのものである。

単なる改善候補ではない。各項目について次を記録する。

- 2026-09-04時点の実DB・API・コード上の根拠
- 欠損と「正常に調査した結果、該当なし」の区別
- 推奨する修正方針と変更候補ファイル
- Yahoo Finance / SEC EDGAR のレート制限を超えない実装条件
- 単体テストだけでなく、本番相当DBで満たす受け入れ条件

本監査ではコード変更、migration、DB更新、commit、pushを行っていない。

## 1. 絶対に維持する制約

1. Yahoo Finance の共有上限 `6.0 req/s` を引き上げない。
2. SEC EDGAR の共有上限 `5.0 req/s` を引き上げない。
3. worker数を増やして共有リミッターを迂回しない。
4. 欠損を0、悪材料、または「該当なし」に変換しない。
5. `not_collected`、`collected_no_finding`、`collected_with_data`、
   `collection_failed`、`not_applicable` を区別する。
6. V4をchampionとして再現可能なまま維持する。V5の低カバレッジ特徴量のfloorを
   下げて、見かけ上有効にしてはならない。
7. FREDの現在値を過去時点で利用してはならない。macro regimeの履歴利用には
   ALFRED等のvintage-awareな入力が必要である。
8. 実データを再取得する前に、既存のraw snapshot、filing、filing section、
   source processing ledgerを再利用できないか確認する。

現在の設定値は `config/collection.yaml` にあり、次のとおりである。

| 設定 | 値 |
|---|---:|
| `max_workers` | 10 |
| `yfinance_requests_per_second` | 6.0 |
| `statement_refresh_grace_days` | 3 |
| `market_session_min_coverage` | 0.90 |
| `edgar.requests_per_second` | 5.0 |
| `edgar.max_workers` | 10 |
| `edgar.max_tracked_tickers` | 300 |

## 2. 監査時点

### 2.1 Git

- 対象: `C:\AI\App_Dev\AutoScreener`
- branch: `main`
- HEAD: `6ab8aa423ace3bffbd60699c813e4a81e69922ca`
- `origin/main`: 同一commit
- worktree: clean
- 増分効率化commit: `a993e28 perf: make daily collection incremental`

### 2.2 最新パイプライン

- 最新scheduled run: 2026-09-04
- status: `succeeded`
- health: `[]`
- 所要時間: 142.9分
- 最新score date: 2026-09-04
- 最新price date: 2026-09-03（監査時点で完了済みの最新米国市場日）
- `/ready`: `status=ready`, `scoring_version=v4`

ただし、このscheduled runは増分効率化commit `a993e28` より前に実行されている。
したがって、増分化後のフルパイプラインについては、単体テストと個別smokeは存在しても、
本番相当の一回完走によるデータ完全性は未受入である。

## 3. 実データで確認した現状

### 3.1 Core市場・財務データ

| 指標 | 件数 | 評価 |
|---|---:|---|
| 非benchmarkのactive ticker | 5,795 | 母集団 |
| 非隔離の収集対象 | 5,770 | 価格カバレッジ分母 |
| 2026-09-03価格あり | 5,384 / 5,770（93.3%） | 386件不足 |
| 追跡対象 | 299 | SEC/Live Intelligence対象 |
| 追跡対象の最新価格あり | 296 / 299（99.0%） | 3件不足 |
| 最新raw snapshot | 5,770 | — |
| 行レベル検証を完全通過 | 4,648 / 5,770（80.6%） | field sanitizationが多い |
| 非空balance sheet | 5,630 / 5,770（97.6%） | 概ね良好 |
| `sharesOutstanding`あり | 5,537 / 5,770（96.0%） | 一部不足 |
| `totalRevenue`あり | 4,850 / 5,770（84.1%） | 不足あり |
| `_statements_as_of`あり | 5 / 5,770 | 本番bootstrap未実施 |

`is_valid=false` はraw行全体が利用不能という意味ではない。現在のsanitizerは疑わしい
fieldを無効化して残りを使用する。このため、受入判定は「valid率」だけでなく、
価格、売上、株式数、財務諸表等のfield別coverageで行うこと。

追跡対象の最新価格不足は次の3銘柄である。

| ticker | 最新price | 最新raw |
|---|---|---|
| PRGS | 2026-09-02 | 2026-09-03 |
| PRLB | 2026-09-02 | 2026-09-03 |
| PRMB | 2026-09-02 | 2026-09-03 |

2026-09-04の収集ではPRDO、PRGS、PRLB、PRLD、PRMBの5銘柄が同じエラーで
`parse_failure` になった。

```text
unclassified exception TypeError: argument of type 'NoneType' is not iterable
```

過去ログにも同じ例外が数銘柄連続で発生する時間帯があり、銘柄固有の恒久的な
データ破損より、yfinance内部のsession/cookie等の共有状態に起因する一時障害が疑われる。
ただし、tracebackを保存して発生元を確認するまでは断定しないこと。

### 3.2 V4スコアリング

| 状態 | 件数 |
|---|---:|
| gate included | 1,266 |
| 分布計算可能 | 1,157 |
| 通常rank対象 | 765 |
| `negative_outlook` | 392 |
| `unmeasurable` | 109 |

`negative_outlook` 392件はデータ欠損ではない。現在のモデル規則による非ランキング状態である。
一方、109件は本当に分布計算不能だが、銘柄別reasonが永続化されていない。

全ユニバースでは`missing_revenue`を含むgate除外が940件ある。included銘柄には
`missing_revenue`は0件なので、現在のランキングへ欠損売上を無理に流し込んではいない。
ただし、収集不足により投資候補になり得た企業を母集団から落としている可能性は残る。

### 3.3 Consensus / Live Intelligence

`GET /api/v1/data-coverage` の確認値:

| dataset | with data | no finding | failed | not collected | 注記 |
|---|---:|---:|---:|---:|---|
| Consensus | 5,759 | 130 | 0 | 0 | 運用上は良好 |
| Guidance | 100 | 0 | 0 | 0 | 分母が実対象を表していない |
| TAM | 3 | 321 | 0 | 1 | 利用可能データが極少 |
| Operating KPI | 125 | 229 | 0 | 1 | 部分coverage |
| Capital allocation | 334 | 20 | 0 | 1 | 追跡範囲は概ね処理済み |
| Management incentives | 330 | 24 | 0 | 1 | 追跡範囲は概ね処理済み |
| Debt | 321 | 33 | 0 | 1 | 追跡範囲は概ね処理済み |
| Milestones | 0 | 0 | 0 | 325 | 現在は手入力前提 |
| Macro exposure | 315 | 10 | 0 | 0 | 現在時点表示には利用可能 |

Coverage APIは、ConsensusとGuidanceについて実テーブルの「行が存在するticker」だけを
`targeted_count`として数える。そのためGuidanceは、追跡299銘柄のうち100銘柄にしか
データがなくても`operational_coverage=100%`に見える。これはデータ不足に加え、
観測可能性の欠陥でもある。

### 3.4 Filing抽出の実欠損

LBTYAは次の状態である。

- `filings`: 852件
- 最新filing date: 2026-09-01
- `filing_sections`: 0件
- capital allocation / debt / management incentives / operating KPIの最新coverage:
  `not_collected`, reason=`no_supported_filing`

実際には10-K、10-Q、8-K、DEF 14A等が存在するため、`no_supported_filing`というreasonは
事実と一致しない。filing本文URL、文書選択、HTML取得、section分割、台帳判定のどこで
落ちているかをticker単位で追跡する必要がある。

また、平日のderived処理対象は`changed_symbols`だけである。新規filing metadataの保存後に
section抽出等が一時失敗した場合、翌日にはそのfilingは「新規」ではなくなるため、
月曜のfull reconciliationまで再処理されない。`source_processing_ledger`の未処理・失敗状態を
日次work queueとして使う必要がある。

### 3.5 V5 shadow feature coverage

最新成功V5 run（as-of 2026-09-04、population 1,266）のcoverage:

| feature | universe coverage |
|---|---:|
| cash conversion | 100.00% |
| incremental ROIC | 99.53% |
| accounting quality | 99.53% |
| per-share economics | 99.53% |
| consensus revision availability | 100.00% |
| capital allocation | 25.83% |
| liquidity / debt maturity | 24.88% |
| reconciliation confidence | 22.83% |
| future dilution capacity | 20.06% |
| litigation | 11.69% |
| customer concentration | 9.79% |
| operating KPI nowcast | 9.64% |
| guidance | 7.74% |
| TAM headroom | 0.24% |
| macro regime | 0.00% |

低coverage特徴量はruntime floorによって無効化されており、欠損を0としてランキングへ混入
させてはいない。この安全装置は維持すること。coverage向上前にfloorを下げてはならない。

macro exposure行は存在するが、`fred_vintage_supported=false`である。したがって現在のFRED
観測を過去時点の特徴量へ流用できず、macro regime 0%は正しい安全動作である。

### 3.6 上場廃止・前方検証

`delisting_events`は94件あるが、次の状態である。

- event type `unknown`: 94 / 94
- confidence `low`: 94 / 94
- settlement valueあり: 0
- last trade priceあり: 0

この状態では買収、破産、清算等のcompeting riskを分類できず、上場廃止銘柄を含む実現
リターンも正しく確定できない。日次ランキングの即時表示よりも、V4/V5の投資家向け検証と
生存者バイアス防止に対する重大な不足である。

## 4. P0修正

### P0-A. 週次財務諸表更新を日次価格collectionから分離する

#### 根本原因

`daily_pipeline.py`はバッチ日が月曜かどうかで`is_weekly`を判定する一方、
`snapshot_collector.collect_one()`の`include_statements`も`snapshot_date.weekday()`が
月曜かどうかで決めている。

増分化後は、月曜の日本時間実行時に米国金曜分がすでに揃っているとcollection全体をskipする。
火曜に米国月曜セッションを収集しても`snapshot_date`は火曜なので、週次財務取得条件に入らない。
その結果、決算イベント対象と初回銘柄以外は週次財務更新が継続的に抜け得る。

#### 推奨設計

価格セッションの完全性と財務諸表refreshを別のwork itemとして管理する。

1. 「当該米国市場週の財務refreshが完了したか」を`collection_cursors`等に保存する。
2. その週で最初に完了した米国市場セッションに対して、週次statement refreshを実行する。
3. priceが完全でもstatement refresh未完了なら、statement専用stageをskipしない。
4. 各tickerの`_statements_as_of`が当該週へ進んだことを確認してからcursorを進める。
5. 部分失敗時は不足tickerだけを次回再試行する。
6. 初回導入時は既存rawのstatement有無を調べ、`_statements_as_of`を安全にbootstrapする。
   実際の取得日を復元できない場合、推測日を現在日にしてはならない。legacy markerまたは
   nullableな観測状態として区別する。

`market_session_date.weekday()`へ単純に置換するだけでは不十分である。火曜に米国月曜分を
収集する場合には改善するが、priceが完全でcollection自体がskipされた場合のstatement仕事が
依然として消えるためである。

#### 変更候補

- `src/autoscreener/batch/daily_pipeline.py`
- `src/autoscreener/batch/market_session.py`
- `src/autoscreener/batch/run_daily_collection.py`
- `src/autoscreener/collectors/snapshot_collector.py`
- `src/autoscreener/db/models.py`
- Alembic migration（専用cursor/statusが必要な場合）
- `tests/unit/test_daily_pipeline.py`
- `tests/unit/test_market_session.py`
- `tests/unit/test_snapshot_collector.py`

#### 受け入れ条件

- 月曜にpriceが100%揃っていても、週次statement refreshが実行される。
- 火曜実行で米国月曜セッションを処理する場合も、同じ週に二重実行されない。
- 途中失敗後は不足tickerだけが再試行される。
- 5,770件の`_statements_as_of` coverageが、失敗・非対応理由を除いて期待件数へ到達する。
- Yahooの実HTTP送信は共有6.0 req/s以下である。
- statement refresh失敗を理由に、直近の有効財務諸表を消さない。

### P0-B. 既知のyfinance一時障害を限定再試行する

#### 推奨設計

1. `parse_failure`保存時に、例外型、発生メソッド、短縮tracebackを保存する。
2. yfinance内部の既知経路から発生した`NoneType is not iterable`のみを一時障害候補にする。
3. cookie/session/crumbを再生成して最大2〜3回再試行する。
4. 共有6.0 req/s limiter、指数backoff、jitter、circuit breakerを必ず通す。
5. 同じエラーが再現する場合は`parse_failure`として表面化し、永久再試行しない。
6. すべての`TypeError`を広くtransient扱いしない。provider schema変更を隠すためである。

#### 変更候補

- `src/autoscreener/collectors/errors.py`
- `src/autoscreener/collectors/yfinance_client.py`
- `src/autoscreener/collectors/snapshot_collector.py`
- `src/autoscreener/batch/parallel_runner.py`
- 関連unit tests

#### 受け入れ条件

- PRDO、PRGS、PRLB、PRLD、PRMBを限定再収集し、結果または説明可能なterminal reasonを得る。
- PRGS、PRLB、PRMBの最新price gapが解消するか、`no_trade`等の正しい状態になる。
- 再試行を含む全HTTPが共有6.0 req/s以下である。
- 不明なTypeErrorは従来どおりparse failureとして検知される。

### P0-C. 上場廃止・清算結果の収集を追加する

#### 推奨設計

- 週次でactive masterと取引所/SEC/信頼できるcorporate action sourceを照合する。
- `unknown`をcash acquisition、stock acquisition、bankruptcy、liquidation、ticker change、
  other等へ分類する。
- `last_trade_date`、`last_trade_price`、`settlement_value_per_share`、`settlement_date`、
  source URL、observed_at、confidenceを保存する。
- sourceが無い場合は推測値を保存しない。
- 前方リターンは実価格または根拠あるsettlementを使い、欠損銘柄を分母から黙って落とさない。

#### 変更候補

- `src/autoscreener/collectors/delisting_source.py`
- `src/autoscreener/db/models.py`
- `src/autoscreener/backtest/`
- `src/autoscreener/batch/daily_pipeline.py`
- CLI / API / tests / migration

#### 受け入れ条件

- 既存94件について分類coverageを件数で報告できる。
- settlement/last tradeが無い件数を明示できる。
- 未分類率が許容閾値以下になるまで、acquisition competing riskを有効化しない。
- delisted銘柄をactive collectionへ戻さず、履歴からも削除しない。

## 5. P1修正

### P1-A. Security masterと価格coverage状態を明示する

現在の`Ticker`にはsecurity typeやexchangeが無く、`select_collectable_symbols()`は主に
`delisted_at`とquarantineだけで選別する。このため、ワラント、権利、unit、OTC、破産後
symbol等が「価格欠損」の分母と毎日の再試行に残る。

追加候補:

- `security_type`: common stock / ADR / preferred / warrant / right / unit / ETF / other
- `exchange`
- `security_name`
- `listing_status`
- `status_observed_at`
- `status_source`
- price coverage reason: `no_trade`, `provider_stale`, `unsupported_security`,
  `collection_failed`, `not_applicable`

履歴行は削除せず、通常日次collectionの対象から外す。普通株なのにYahooだけ欠損する場合は、
レート予算を明示したfallback sourceを検討する。

### P1-B. Source processing ledgerを日次retry queueにする

平日derived処理を`changed_symbols`だけで決めず、次を和集合にする。

- 新規・更新filingがあるticker
- ledgerに成功記録が無いsource/processor/version
- retryable failureでbackoff期限を過ぎたsource
- processor versionが上がったsource

成功または確定したno-findingの場合だけ完了扱いにする。URL未確定、空本文、HTTP一時障害、
parse失敗はno-findingへ変換しない。LBTYAを最初の受入用tickerにする。

### P1-C. V4 `unmeasurable_reason`を永続化する

109件を少なくとも次へ分類する。

- missing/stale price
- missing shares
- missing revenue/gross profit/history
- unconvertible currency
- nonpositive/invalid enterprise value
- unsupported accounting structure
- other calculation failure

pipeline result、API、詳細画面、coverage集計から銘柄別に確認可能にする。
理由を保存するために、既存`Score`へNULL行を作るか、専用diagnostic tableを作るかは、
append-only/PIT性を保てる方を選ぶ。

### P1-D. Coverage APIの分母を実対象集合にする

`targeted_count=len(latest_rows)`ではなく、そのバッチで凍結した対象集合を分母にする。
Consensus/Guidanceについても、対象だが行が無いtickerを`not_collected`または
`collected_no_finding`としてcoverage ledgerへ記録する。

受け入れ条件:

- Guidance 100件だけを分母にして100%と表示しない。
- universe、eligible、targeted、attempted、with data、no finding、failed、not collectedが
  相互に整合する。
- 追跡299件と全ユニバース5,889件のどちらを表すかAPIで明示する。

### P1-E. Form 4を新規filingから日次抽出する

現在insider collectionは週次だが、Form 4 metadataは日次filing増分に含まれる。
新規accessionだけを日次処理すれば、SECリクエストの大幅増加なしに最大約6日の遅延を減らせる。
既存document cacheを優先し、5.0 req/sを維持する。

## 6. P2修正

### P2-A. XBRL coverageの理由分類

追跡299件のうちXBRL factが無いtickerが存在する。外国発行体、companyfacts非対応、CIK mapping、
IFRS tag、期間整合、取得失敗を区別し、単純な0または「データなし」にしない。

### P2-B. Live Intelligence coverageの拡充

優先順は次のとおり。

1. Operating KPI / guidance: 10-K、10-Q、8-K EX-99の保存済みsectionから抽出範囲を広げる。
2. Dilution: shelf、ATM、convertible、option/warrant、未行使可能株式を原文根拠付きで保存する。
3. Customer concentration / litigation: no-findingと未走査を厳密に分ける。
4. TAM: 会社開示が無い場合、根拠の無い数値を生成しない。外部researchを使う場合は
   source、as-of、地域、単位、二重計上防止を必須にする。
5. Milestones: 現在の0/325は手入力前提として正しい。自動化する場合も「候補の提案」として
   保存し、ユーザー承認前に確定milestoneへ昇格させない。

### P2-C. Macro vintage対応

ALFRED等から、評価日時点で利用可能だった観測vintageを保存する。現行FREDの値を過去へ
retrofitしてはならない。vintage coverageがfloorを満たすまでmacro regimeはforward shadow only
とする。

## 7. 推奨実装順序

1. P0-A 週次statement refresh分離とbootstrap
2. P0-B yfinance限定retry、3追跡tickerの回収
3. P1-C unmeasurable reason永続化
4. P1-D coverage分母・status ledger修正
5. P1-A security master / no-trade状態
6. P1-B EDGAR retry queue、LBTYA修復
7. P1-E Form 4日次増分
8. P0-C delisting/settlement収集
9. P2-A XBRL理由分類
10. P2-B Live Intelligence拡充
11. P2-C macro vintage対応

P0-Cはバックテスト・モデル昇格判断に着手する前には必須である。日次ランキングの直近欠損
回収だけを目的にする場合はP0-A/P0-Bを先行できるが、V5の評価完了を宣言してはならない。

## 8. テスト計画

### 8.1 Unit / integration

- 月曜price完全時でもstatement stageが走る。
- 火曜に米国月曜セッションを処理しても週1回だけ実行される。
- 部分失敗後の再開で成功tickerを取り直さない。
- legacy raw、markerありraw、markerなしraw、新規ticker、決算直後tickerを網羅する。
- yfinance既知TypeErrorのみ限定retryされる。
- rate limit、jitter、backoff、circuit breakerをモック時刻で検証する。
- ledger succeeded/no-finding/failed/unprocessed/version changeの全遷移を検証する。
- coverage集計の各count合計と対象集合が一致する。
- unmeasurable reasonがPITのas-of日を超えて参照されない。
- delisting settlementを用いるforward returnと、未確定時の保留を検証する。

### 8.2 実DB受け入れ

テスト成功だけで完了にしない。次を実DBで確認する。

1. migration前backupが非ゼロでgzip/pg_dumpとして読める。
2. migration headが期待revisionである。
3. APIプロセスとscheduled taskが意図したcheckout/commitを実行している。
4. optimized pipelineを少なくとも通常日1回、週次refresh対象日1回完走させる。
5. `pipeline_runs` / `pipeline_stage_runs`でstatus、reason、所要時間を確認する。
6. 価格、raw、statement marker、shares、revenueのcoverageを実数で再計測する。
7. PRGS、PRLB、PRMB、LBTYAを代表tickerとしてAPI・DB双方で確認する。
8. `/ready`、`/api/v1/data-coverage`、ランキング、代表詳細endpointを確認する。
9. V4のscore件数・順位が意図せず変わっていないことを確認する。
10. V5のfeature floorが維持され、低coverage特徴量が引き続き無効であることを確認する。
11. Yahoo 6.0 req/s、SEC 5.0 req/sを実HTTP境界の計測で確認する。
12. frontend test / lint / buildを実行する。

## 9. 再監査用の最小確認箇所

### コード

- `src/autoscreener/batch/daily_pipeline.py`
  - `is_weekly`
  - market session skip
  - `changed_symbols` / `derived_symbols`
- `src/autoscreener/collectors/snapshot_collector.py`
  - `_STATEMENTS_AS_OF_KEY`
  - `include_statements`
  - stale priceとshares carry-forward
- `src/autoscreener/collectors/errors.py`
  - `classify_exception`
- `src/autoscreener/batch/run_daily_collection.py`
  - `select_collectable_symbols`
- `src/autoscreener/api/routes.py`
  - `/data-coverage`
- `src/autoscreener/scoring/engine.py`
  - `unmeasurable`
- `src/autoscreener/scoring/v5/feature_registry.py`
  - coverage floorとmacro vintage要件
- `src/autoscreener/db/models.py`
  - source ledger、coverage、delisting、score関連table

### API

```powershell
Invoke-RestMethod http://127.0.0.1:8000/ready
Invoke-RestMethod http://127.0.0.1:8000/api/v1/data-coverage
```

### Git / process identity

```powershell
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
git log --oneline -10
```

DB確認にはリポジトリの`autoscreener.db.session.get_engine()`または`session_scope()`を使い、
`.env`の接続情報を標準出力へ出さないこと。

## 10. 完了判定

次をすべて満たすまで「データ不足を解消した」と報告しない。

- 週次statement refreshが市場セッションskipと独立して完走する。
- statement観測日が全対象で説明可能になっている。
- 追跡対象の最新価格欠損が0、または全件に説明可能なterminal coverage reasonがある。
- 109件のunmeasurableを銘柄別reasonで説明できる。
- Guidanceを含むcoverage APIの分母が実対象集合と一致する。
- LBTYAのsection 0件について原因と最終状態が説明できる。
- ledgerの未処理・retryable failureが日次で自己回復する。
- 上場廃止データの分類・settlement不足を数値で明示でき、未完成のままモデル昇格しない。
- レート上限を引き上げていない。
- V4 championの再現性を維持している。
- 実DB、API、scheduled process、frontendまで確認している。

## 11. 関連文書

- `docs/daily_pipeline_throughput_plan_2026-09-04.md`
- `docs/daily_pipeline_incremental_efficiency_2026-09-04.md`
- `docs/model_v5_validation.md`
- `docs/racr_integrated_redesign_plan_2026-09-04.md`
