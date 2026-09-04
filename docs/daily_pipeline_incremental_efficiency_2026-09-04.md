# 日次パイプライン増分化（2026-09-04）

## 結論

日次パイプラインを「全銘柄・全資料を毎回取り直す」方式から、取引日・変更銘柄・
未処理原文だけを処理する方式へ変更した。外部サービスへの送信上限は変更していない。

- Yahoo Finance: `6.0 req/s`
- SEC EDGAR（全文検索を含む全経路で共有）: `5.0 req/s`
- collection / SEC worker: `10`（待ち時間を埋めるだけで、共有リミッターを迂回しない）

## 実装した最適化

### 1. 米国市場セッション単位の実行判定

`exchange_calendars` の XNYS カレンダーで、実行時点ですでに終了している最新の
米国取引日を判定する。日本の曜日だけでは判定しないため、米国休場日を正しく飛ばし、
土曜09:00 JSTには金曜分を取得できる。

対象取引日の価格がすでにある銘柄は再取得しない。部分失敗後は不足銘柄だけを補完し、
90%到達を理由に残り10%を捨てない。全銘柄が揃ったときだけ collection、consensus、
gates、scoring、forward validation、macro exposure、V5 shadow を正常な skip とする。

### 2. 発行済株式数の週次・イベント時取得

価格は取引日ごとに取得するが、`get_shares_full()` は次の場合だけ呼ぶ。

- 観測値がまだ無い
- 米国市場の週初セッション
- 直近観測から7日経過
- 決算再取得期間
- 10-K / 10-Q / 20-F / 40-F / S-3 / 424B / 8-K の新規提出後
- 隔離からの復帰

通常日は直近値を持ち越す。`shares_observed_at` と
`shares_coverage_status` を別に保存するため、持ち越しを当日観測と誤認しない。

### 3. SEC提出書類のdaily index増分取得

平日は EDGAR daily master index を読み、追跡対象のCIKに変更があった銘柄だけ
`submissions` を取得する。カーソル以後に停止期間があれば全日を順に追いつく。
09:00 JST時点では直前の米国日のindexが未確定な場合があるため、確定済みの2日前までを
処理し、直前日ぶんは次回実行で拾う。
検索または保存に失敗した実行ではカーソルを進めない。月曜は従来どおり全追跡銘柄を
照合し、index遅延や訂正を回収する。

### 4. 訴訟全文検索のCIK一括化

銘柄ごとに同じ検索語を投げる方式を廃止し、CIKを50件ずつまとめて検索する。
300銘柄・7検索語なら、ヒットのページングを除く基礎リクエストは最大約2,100本から
42本になる。成功カーソルから2日重複して検索し、境界の遅延反映を拾う。

### 5. 下流抽出の変更銘柄限定と処理台帳

filing sections、guidance、customer concentration、dilution、investment
intelligence、market opportunity は新規提出があった銘柄だけを日次処理する。
月曜は全追跡銘柄で照合する。

`source_processing_ledger` に原文ID、抽出器、抽出器バージョン、成功・該当なしを保存する。
同じ原文を毎日再解析せず、抽出器バージョンを上げれば安全に再処理できる。URL未確定や
空レスポンスは恒久的な「該当なし」にせず、次回再試行する。

## 障害時の規則

- `collection_cursors` は対象処理の全保存成功後にのみ進める。
- pipelineのresumeでは、成功済みcollectionを再実行しない一方、未完了のgates・
  scoring等は継続する。
- 意図した休場日skipを `scoring_skipped` エラーとして扱わない。
- 403 / 429 / 503 の共有冷却、指数バックオフ、サーキットブレーカーは維持する。
- 外部レート上限はworker数とは独立であり、workerを増やしても超過できない。

## DB変更

Alembic `a6d8e0f2b4c6` で次を追加する。

- `price_snapshots.shares_observed_at`
- `price_snapshots.shares_coverage_status`
- `collection_cursors`
- `source_processing_ledger`

既存の発行済株式数は、移行時に `legacy_observation` として元の取引日を観測日に設定する。

## 受け入れ条件

- 専用テストDBで全Pythonテストが成功すること。
- migration headが `a6d8e0f2b4c6` であること。
- 本番DBの移行前バックアップが非ゼロで、gzip / pg_dumpとして読めること。
- 実DBで市場セッション判定、増分カーソル、skip理由、stage statusを確認すること。
- frontendのtest / lint / buildが成功すること。
- push後にローカル `main` と `origin/main` が一致すること。
