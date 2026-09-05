# 上場廃止前後の証拠収集を止めない — 収集層の是正(2026-09-05)

**先行資料:** `docs/delisting_label_backfill_2026-09-04.md`(本作業の一次情報源。
根本原因の調査記録)。本ドキュメントはその §9「次にこの領域へ着手する者への
実務的な示唆」の1.・2.を実装した記録。

**対象コミット時点:** `main` の `4d92fbc`(delisting classification groundworkが
マージ済み)からの分岐。`codex/daily-pipeline-data-completeness` /
`codex/daily-pipeline-p0-refresh` および直近の `perf: make daily collection
incremental`(`a993e28`)と同じファイル(`collect_filings.py`)を触るため、
diffは意図的に最小限に留めた(詳細は末尾「あえて触らなかったもの」)。

## 1. 何が壊れていたか(再掲)

`collect_filings.py` / `run_daily_collection.py` は `Ticker.delisted_at` が
確定した瞬間にそのCIKの収集を止める設計になっていた。ところが上場廃止の
**原因を語る決定的なフォーム**(Form 25/15そのもの、8-K Item 1.03=破産・
2.01=資産処分完了・3.01=上場基準抵触通知、DEFM14A、Schedule 13E-3)は、
まさに `delisted_at` が確定する前後に集中して提出される。加えて
`TRACKED_FORMS` には `SC 13E3`/`DEFM14A` がそもそも入っていなかった。結果、
dev DBの94件の廃止イベントは全件 `event_type='unknown'` のまま
(`ticker_id` で正しくJOINしても証拠が1件も無い)。

## 2. 直した内容

### 2.1 `TRACKED_FORMS` に不足分を追加(`src/autoscreener/batch/collect_filings.py`)

追加したフォームは、既存の `collectors/delisting_classification.py` が
実際に探しに行くフォーム集合(`_DEREGISTRATION_FORMS` /
`_GOING_PRIVATE_FORMS` / `_MERGER_PROXY_FORMS`)と1対1で一致させた
(将来どちらかを変えたらもう片方も更新する必要がある、とコード内コメントに
明記):

| 追加フォーム | 何を語るか |
|---|---|
| `25` | 取引所発の上場廃止届(元々 `25-NSE` はあったが素の `25` が抜けていた) |
| `15-12G` | 登録抹消(元々 `15-12B` のみ追跡、株式クラス違いの `15-12G` が抜けていた) |
| `15F-12B` / `15F-12G` | 外国民間発行体の登録抹消 |
| `SC 13E3` / `SC 13E-3` | 非公開化(going-private)届出書。EDGARの正規化表記はハイフン無しが基本だが表記揺れに備え両方 |
| `DEFM14A` | 合併の委任状勧誘書類(対価が現金/株式か、金額を含みうる) |

8-K自体は元から追跡対象であり、Item 1.03/2.01/3.01はどれも8-Kの`items`配列に
乗る(`edgar_client.fetch_filings`はフォーム単位でしか絞り込まず、item番号
では絞らない)。したがって8-K向けに追加のフォーム登録は不要——**既に
収集されているが、収集が`delisted_at`到達で止まっていたために取りこぼして
いた**、というのが正確な理解。

### 2.2 猶予期間 `POST_DELISTING_FILING_WINDOW_DAYS = 90`

**根拠(推測ではなくSECの制度上の期限から逆算):**

- SEC Rule 12d2-2(d)(1):取引所によるForm 25提出から上場廃止の効力発生
  まで**10日**。
- Exchange Act Section 12(b)/12(g):登録抹消(Form 15の効力発生)まで
  **最大90日**。
- 既存の `collectors/delisting_source.py` の `DELISTING_TRADING_GRACE_DAYS
  = 30` は別の窓(「価格が廃止後も動き続けているか」を見る誤検出対策)で、
  実測の最終取引日ズレ分布(0日以下5件・7〜17日7件・その後33日以降に密集)
  を根拠にしている。フォームの提出はその最終取引よりさらに後(合併委任状の
  確定・破産手続きの追加8-K等)まで続きうるため、価格用の30日をそのまま
  流用するのは短すぎると判断した。

**90日を採用した理由:** Form 15の効力発生上限(Section 12(b)/12(g))と
同じ長さに揃えることで、「登録抹消の手続きが完全に終わるまでは証拠を
拾いに行く」という一貫した基準になる。合併委任状(DEFM14A)の確定が
これよりさらに遅れるケースはなお残りうるが、根拠のない値(180日・365日
など)を足で伸ばすより、制度上の期限に揃えた値のほうが正当化できる、
という判断。

### 2.3 `select_tracked_tickers` に第4のカテゴリを追加

従来の追跡対象は「保有銘柄 ∪ 直近スコアのランキング上位N件 ∪
研究ノートのある銘柄」の和集合だった。上場廃止された銘柄は通常、保有が
クローズされ・ランキング圏外に落ち・ノートも更新されなくなるため、
`delisted_at`確定と同時に**この3カテゴリのどれからも自然に脱落し、収集が
事実上止まっていた**(明示的な除外フィルタが主因ではなく、追跡対象の
選定ロジックが拾わなくなることが主因、というのが実装後の正確な理解)。

第4カテゴリとして「`delisted_at`確定後90日以内・CIK保有」を追加した
(`_recently_delisted_trackable_symbols`)。日次インデックス最適化
(`perf: make daily collection incremental`, `a993e28`)経路でも、保有銘柄・
ノート銘柄と同様に「その日の日次インデックスに出現したCIKか」に関わらず
毎回 `fetch_filings` を通す優先集合に含めた——猶予期間は最長90日・対象は
通常ごく少数(実測94件/約4年)であり、1銘柄あたりのリクエスト数は
従来どおり1回のままなので `edgar.requests_per_second` を押し上げない。

### 2.4 CIK共有の罠への対処(収集レイヤーでの新規ガード)

`docs/delisting_label_backfill_2026-09-04.md` §2 は「`filings`を`cik`で
JOINすると、現役銘柄TDWの通常運用中の提出書類が廃止済みTDGMW(同一CIK)の
証拠として誤帰属する」ことを**分類ロジックの読み取り側**で発見し、
`ticker_id`のみのJOINで対処済みだった。

しかし本タスクで第4カテゴリを実装する過程で、**より深刻な変種**に気づいた:
もし廃止済みTDGMWを追跡対象に含めて `EdgarClient.fetch_filings(ticker.cik)`
を呼べば、それはCIK単位のAPIである以上、**現役TDWの提出書類そのものが
返り**、それを`_upsert_filings`が「TDGMWの`ticker_id`」で書き込んでしまう。
これは分類ロジックの`ambiguous_shared_cik`フラグ(DB反映を止めるだけ)では
防げない——**収集の時点で`filings`テーブル自体に汚染が書き込まれてしまう**
ため、後から読み取り側だけ直しても手遅れになる。

対処:`_recently_delisted_trackable_symbols`は、候補の廃止銘柄と同じCIKを
持つ**現役銘柄(`delisted_at IS NULL`)が存在する場合、その候補を追跡対象
から除外する**。これにより、シェアCIKパターンの銘柄については収集自体を
行わない(証拠を諦める代わりに、誤帰属を作らない)。

## 3. テスト(`tests/unit/test_collect_filings.py`)

新規4件(既存7件は無変更、回帰なし):

1. `test_tracked_forms_include_post_delisting_evidence_forms` — 追加した
   7フォーム(`25`/`15-12G`/`15F-12B`/`15F-12G`/`SC 13E3`/`SC 13E-3`/
   `DEFM14A`)が `TRACKED_FORMS` にあること、既存フォームが消えていないこと。
2. `test_select_tracked_tickers_includes_recently_delisted_within_window_and_excludes_outside_it`
   — 窓の境界:`delisted_at`確定後10日の銘柄は追跡対象に入り、
   `POST_DELISTING_FILING_WINDOW_DAYS + 30`日前に廃止された銘柄(=既存94件が
   置かれている状況の模擬)は入らないこと。
3. `test_select_tracked_tickers_excludes_delisted_ticker_sharing_cik_with_active_ticker`
   — TDW/TDGMWパターンの回帰テスト(§2.4のガード)。
4. `test_collect_filings_attributes_post_delisting_filings_to_the_delisted_tickers_own_ticker_id`
   — 収集した行が正しく`ticker_id`に紐づくこと(取り違えでない)のend-to-end確認。

既存の `tests/unit/test_delisting_classification.py`(CIK共有の分類側回帰
テスト、14件)は無変更・全件パス。

**実測結果:**
```
TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
uv run pytest tests/ -q
```
`1194 passed, 0 failed`(基準1190 + 新規4、失敗0)。

## 4. これで直るもの・直らないもの(正直な棚卸し)

**直るもの(今後発生する上場廃止に対して):** 今後 `delisted_at` が
確定する銘柄は、確定後90日間は引き続き `filings` の収集対象になり、
Form 25/15・8-K Item 1.03/2.01/3.01・DEFM14A・SC 13E3が収集され次第、
既存の `classify_stored_delisting_events` / `apply_classifications`
(`collectors/delisting_classification.py`)が自動的に(`bankruptcy`のみ
自動分類、他は証拠を残しつつ`unknown`のまま人間の確認待ち)処理できる
状態になる。

**直らないもの:** **dev DBに既にある94件の `delisting_events` は、この
修正では1件も新たに分類可能にならない。** 理由は単純で、94件は全て
`delisted_at` が既に(2022-07〜2026-08の範囲で)確定済みであり、うち
最も新しいものでも本修正のマージ時点でとうに90日の猶予期間を過ぎている
——今回追加したのは**将来に向けた収集の継続**であり、過去に遡って
Form 25/15や8-Kを"取り戻しに行く"ロジックではない(EDGARの
`filings.recent`は直近1000件しか返さないため、多くの発行体では過去分の
決定的フォームは既に`recent`ウィンドウの外に出ており、たとえ今から
ライブ取得しても再現できない可能性が高い——`docs/delisting_label_backfill_2026-09-04.md`
§4参照)。

したがって:**94件は本修正の後もなお `event_type='unknown'` のままであり、
分類可能にするための追加作業(過去分の`full-index`再走査によるフォーム
特定・EDGAR提出書類の本文からの決済額抽出等)は本タスクの範囲外として
明示的に未着手のまま残す。**

## 5. あえて触らなかったもの(並行作業との衝突回避)

- **`run_daily_collection.py` は変更していない。** このファイルの
  `Ticker.delisted_at.is_(None)` フィルタはyfinance経由の**株価スナップショット**
  収集(`collect_one`)を対象にしており、本タスクが扱うSEC**提出書類**収集
  (`collect_filings.py`)とは別の関心事。廃止済み銘柄の株価をyfinanceに
  問い合わせ続けても404が返るだけで実益が無く、この設計自体は妥当と判断した。
  加えてこのファイルは `codex/daily-pipeline-data-completeness` /
  `codex/daily-pipeline-p0-refresh` の作業領域と重なるため、範囲外の変更で
  衝突を増やさない選択をした。
- **決済額(`settlement_value_per_share`)の自動抽出はしていない。**
  `docs/delisting_label_backfill_2026-09-04.md` §6.1の既存の判断(決済額を
  伴わない`cash_acquisition`/`stock_acquisition`の自動付与は
  `backtest/runner.py`に-100%として誤読される)をそのまま維持し、本タスクは
  「証拠を集める」ところまでに留めた。フォーム本文のパースは別タスク。
- **`collectors/delisting_classification.py` のtaxonomy・分類ロジックは
  無変更。** 既に2026-09-04時点でTDW/TDGMWの回帰テストとともに実装済みで
  あり、本タスクが追加した収集経路もその`ticker_id`限定JOINの設計にそのまま
  乗る(収集側のCIK共有ガードは分類側のガードを補完するもので、置き換える
  ものではない)。
- **収集ループ自体の並列化・リファクタリングはしていない。** `select_tracked_tickers`
  へのカテゴリ追加のみで、既存の逐次ループ構造・日次インデックス最適化の
  構造は変えていない。

## 6. 変更ファイル

| ファイル | 変更 |
|---|---|
| `src/autoscreener/batch/collect_filings.py` | `TRACKED_FORMS`に7フォーム追加、`POST_DELISTING_FILING_WINDOW_DAYS`定数、`_recently_delisted_trackable_symbols`新規関数、`select_tracked_tickers`/日次インデックス優先集合への統合 |
| `tests/unit/test_collect_filings.py` | 新規4テスト(§3) |
| `docs/post_delisting_evidence_collection_2026-09-05.md`(本ファイル) | 本記録 |
