# 詳細画面「未取得」／資本配分が空になる原因と修正手順（2026-09-01 引継ぎ）

**対象:** 銘柄詳細の Live Investment Intelligence（TAM・テーゼのマイルストーン・M&A competing risk・資本配分）
**引継ぎ先:** Sonnet
**前提文書:** `docs/live_intelligence_missing_data_remediation_plan_2026-09-01.md`（実装仕様の正本。本書はその**現況差分と着手順**）

---

## 0. 一行結論

**修正コードはすでに `codex/macro-coverage-status` ブランチに存在し、そのコードで本番DBへ書き込み済みだが、`main` にマージされていない。**
ユーザーが見ている API と UI は `main`（＝修正前）なので、DBにデータがあっても「未取得」と表示され、資本配分は空表になる。
新規実装を書き起こす前に、**まずマージすること**。

さらに、DBは既にブランチ側のマイグレーション `e9b1c3d5f7a9` まで進んでおり、`main` からは
`alembic current` すら失敗する（後述 2.4）。DBとコードの整合が壊れているため、マージは任意ではなく必須。

---

## 1. 症状 → 原因マップ

| ユーザーが見た症状 | 直接原因（`main` のコード） | 該当箇所 | ブランチでの対処 |
|---|---|---|---|
| **資本配分が表示されない**（見出しだけ出て中身が空） | `DataBlock` が `Array.isArray(value)` と `typeof value === "object"` の値を全部捨てる。capital-allocation の `data` は `{three_year_totals: dict, events: array}` の2キーだけなので**全行が捨てられ、空の table が描画される** | [InvestmentIntelligenceSections.tsx:56-58](frontend/src/components/InvestmentIntelligenceSections.tsx#L56-L58) | `CapitalAllocationSection` を新設し、3年集計表＋イベント表を専用描画 |
| **TAM が未取得** | ①`main` に `MarketOpportunityEstimate` の writer が存在しない ②`get_market_opportunity` が `LiveDatasetCoverage` を参照しないため、行が無いと**常に** `not_collected`＝「未取得」になる（DBには `market_opportunity` の coverage が299行あるのに無視される） | [routes.py:3548-3556](src/autoscreener/api/routes.py#L3548-L3556) | `collect_market_opportunity.py` を新設、endpoint は `coverage=_dataset_coverage(...)` を渡す |
| **テーゼのマイルストーンが未取得** | `research/` に実ノートが無く（`README.md` と `TEMPLATE.md` のみ）`thesis_milestones` は0行。**これは正しい欠損**だが、`main` は理由を出さず「未取得」としか言わない | [collect_investment_intelligence.py:129-152](src/autoscreener/batch/collect_investment_intelligence.py#L129-L152) | `not_collected / user_input_missing` を記録し、UIに「ユーザー未設定 — research/&lt;TICKER&gt;.md に入力してください」と出す |
| **M&A competing risk が未取得** | `DelistingEvent` の**個別銘柄行は、上場中の銘柄には構造的に存在しない**。`_intelligence_response` は `rows` が空だと `not_collected` に落とすため、母集団統計（592件）を計算済みでも UI が `data` を見る前に「未取得」で打ち切る | [routes.py:3632-3639](src/autoscreener/api/routes.py#L3632-L3639) | `population_statistics` と `ticker_events` を分離し、母集団があれば `collected_with_data` |
| （併発）上位銘柄で**全セクションが空** | SEC本文の収集対象が上位300銘柄に限定。CARG / AMPL / BOOT は `filing_sections` 0件 → 資本配分・KPI・債務・経営陣も全部空 | [collect_filing_sections.py:37](src/autoscreener/batch/collect_filing_sections.py#L37) | 対象母集団を pipeline 冒頭で1回凍結し、対象外は `outside_collection_scope` と表示 |
| （併発）APIエラーが「未取得」に化ける | `Promise.allSettled` の rejected を捨て、`data[key]` 未定義＝「未取得」表示 | [InvestmentIntelligenceSections.tsx:82-87](frontend/src/components/InvestmentIntelligenceSections.tsx#L82-L87) | `SectionState` discriminated union（loading / loaded / error）へ置換 |

---

## 2. 再現証拠（2026-09-01 実測）

### 2.1 `main` のendpointを本番DBに対して直接呼んだ結果

```
CARG   market-opportunity   not_collected          data=list[0]
CARG   capital-allocation   not_collected          data=['three_year_totals','events']
CARG   thesis-milestones    not_collected          data=list[0]
CARG   mna-history          not_collected          data=['historical_acquisition_count',...]   <- 母集団592件を計算済みなのに not_collected
XERS   market-opportunity   collected_with_data    data=list[3]
XERS   capital-allocation   collected_with_data    data=['three_year_totals','events']         <- statusは正常。UIだけが空表になる
XERS   thesis-milestones    not_collected          data=list[0]
XERS   mna-history          not_collected          data=[...]
```

**XERS の capital-allocation は `collected_with_data` を返している。** 表示されないのは 100% frontend の `DataBlock` が原因。

### 2.2 DBはコードより先行している（重要）

`main` には writer が存在しないテーブルに、2026-09-01 13:35〜13:53 UTC の行がある。

| テーブル | 行数 | `main` に writer | 実際に書いたもの |
|---|---:|---|---|
| `market_opportunity_estimates` | 4 | **なし** | ブランチの `collect_market_opportunity.py` |
| `macro_exposure_snapshots` | 870 | **なし** | ブランチの `collect_macro_exposure.py` |
| `liquidity_facilities` | 308 | **なし** | ブランチの collector |
| `delisting_events` | 592 | なし（`tickers.delisted_at` のみ） | ブランチの `backfill-delisting-events` |
| `capital_allocation_events` | 7,221 | あり（ただし4語・section毎1件のみ） | ブランチの拡張extractor |

決定的な裏付け: DBには `divestiture` 269件 / `equity_raise` 136件 / `debt_raise` 22件があるが、
`main` の [`extract_capital_events`](src/autoscreener/screening/investment_intelligence_extract.py#L83-L95) は
`buyback / acquisition / dividend / capex` の4種しか生成できない。
→ **DBの中身を見て「writerは実装済み」と判断してはいけない。`main` のコードだけを真実とみなすこと。**

### 2.3 coverage台帳は既に入っているのに `main` が読んでいない

```
market_opportunity : collected_no_finding 258 / collected_with_data 2 / not_collected 39
thesis_milestones  : not_collected 299（reason=user_input_missing）
macro_exposure     : collected_with_data 290 / collected_no_finding 10 / not_collected 10
```

`main` の `get_market_opportunity` / `get_thesis_milestones` / `get_macro_exposure` / `get_mna_history` は
`_dataset_status()` を呼んでいないため、この台帳を一切使っていない。

### 2.4 マイグレーションが `main` から壊れている

```
$ python -m alembic current
FAILED: Can't locate revision identified by 'e9b1c3d5f7a9'
$ python -m alembic heads
c9f5a7b2d4e6 (head)
```

DBは `e9b1c3d5f7a9`（ブランチの `live_coverage_contract`）まで進んでいるが、`main` にそのファイルが無い。
`live_dataset_coverage` には既に `reason_code / reason_detail / attempted_at / source_scope / retryable` が存在する。

---

## 3. 修正手順（この順で実施する）

### Step 1 — ブランチを `main` へマージする【最優先・これだけで大半が直る】

```
git merge --no-ff codex/macro-coverage-status
```

- ブランチは `4436aa0` 起点、`main` はその後 `6e86cd4` / `2b86ef6`（chart/theme系）だけ進んでいる。
- 変更ファイルは重複しない。`git merge-tree` で**コンフリクト0件を確認済み**。
- 取り込まれるもの: `src/autoscreener/coverage.py`（CoverageStatus / ReasonCode の正本）、
  `collect_market_opportunity.py`、`collect_macro_exposure.py`、拡張extractor、
  routes/schemas の coverage 伝播、`CapitalAllocationSection`、`SectionState`、
  migration `e9b1c3d5f7a9`、CLI 3コマンド、daily_pipeline への組込み。

**受け入れ条件**
- `python -m alembic current` が `e9b1c3d5f7a9` を返す（エラーにならない）。
- `pytest` 全通過。
- `cd frontend && npm install && npm run build` が通る（ブランチが vitest を追加しているため `npm install` 必須）。
- `npm test` で `InvestmentIntelligenceSections.test.tsx` が通る。

### Step 2 — 画面で症状が解消したことを実機確認する

API を起動し、以下を目視確認する。**passing tests を「直った証拠」にしないこと。**

| 銘柄 | 確認項目 | 期待表示 |
|---|---|---|
| XERS | 資本配分 | 3年集計表＋イベント表が描画される（空表でない） |
| XERS | TAM | 3件の estimate が表示される |
| XERS | M&A competing risk | 母集団統計が表示される（「未取得」でない） |
| 任意 | テーゼのマイルストーン | 「ユーザー未設定 — research/&lt;TICKER&gt;.md に入力してください」 |
| CARG / AMPL / BOOT | 全セクション | 「現在の収集対象外です」（無言の「未取得」でない） |
| 任意 | APIを落とした状態 | 「取得APIエラー」（「未取得」に化けない） |

### Step 3 — マージ後も残るデータ品質の欠陥を潰す

マージしても以下は残る。**これはブランチのバグであり、直す必要がある。**

1. **TAM抽出の誤値** — `collect_market_opportunity.py` の `_SCALE` に `trillion` が無く、
   単位語が無い一致もそのまま採用される。実測で `XERS tam_value = 1.0`（＝「$1 trillion」等を1ドルとして保存）
   という明らかな誤値が入っている。
   - `_SCALE` に `trillion: 1e12` を追加。
   - 単位語が取れない一致は保存しない（保存するなら `confidence` を下げ、画面で要検証と明示する）。
   - 既存4行を再検証し、誤値は削除してから再収集する。
2. **M&A分類が全件 `unknown`** — `delisting_events` 592件すべて `event_type='unknown'` のため、
   `acquisition_share` が 0.0 になり「買収比率0%」という**誤った断定**が画面に出る。
   - 全件 `unknown` のときは `acquisition_share` を `None` にし、UIに「分類未実施」と出す。
   - 分類実装は元計画 §8.2「将来分類」に従う（推測でacquisition/bankruptcyに振らない）。
3. **TAMのヒット率が実質ゼロ** — 300銘柄中 `collected_with_data` は2銘柄のみ。
   正規表現が10-K本文のTAM記述に届いていない。元計画 §6.2 の入力経路（research note の
   `market_opportunity` front matter、bottom-up components）を実装しないと埋まらない。
4. **収集対象が300銘柄** — ランキング上位でも `filing_sections` 0件の銘柄が存在する
   （BVN / PAGS / OGC / TFPM / NEXA は外国民間発行体で 20-F/40-F を提出するため、
   対象フォーム `10-K/10-Q/8-K/DEF 14A` に一致しない）。元計画 §4.4 のフォーム対応拡張が必要。

### Step 4 — 収集を再実行して台帳とコードを一致させる

マージ後のコードは既存行を書いたコードと同一なので冪等に再実行できる。Step 3 の修正後に実行する。

```
uv run autoscreener collect-market-opportunity
uv run autoscreener collect-macro-exposure
uv run autoscreener collect-investment-intelligence
```

**受け入れ条件:** 実行前後で `live_dataset_coverage` の dataset別 status内訳を比較し、
`not_collected` が減った／理由が付いたことを件数で示す。「テストが通った」では受け入れない。

---

## 4. やってはいけないこと

- **新規に writer を書き起こすこと。** 既に `codex/macro-coverage-status` にある。二重実装になる。
- **DBの行数を見て「実装済み」と判断すること。** DBはコードより先行している（§2.2）。
- **`main` 側で新しい migration を切ること。** DBのheadが `main` に無いため整合が取れない。マージが先。
- **欠損を0や「該当なし」に丸めること。** `not_collected` / `collected_no_finding` / `collection_failed` /
  `not_applicable` を混同しない。
- **マイルストーンをアプリから自動生成すること。** `research/<TICKER>.md` はGit管理の正本で、ユーザー入力が唯一の源。
- **ランキングやスコアに触ること。** Live Intelligence は `Not used in ranking` のまま。

---

## 5. 参照

- 実装仕様の正本: `docs/live_intelligence_missing_data_remediation_plan_2026-09-01.md`
  （本書 Step 3 以降は同文書の Phase 2〜5 に対応）
- 未マージブランチ: `codex/macro-coverage-status`（`45cb39b`, `6ce2a1a`）
- worktree: `C:/Users/oh12k/.codex/worktrees/c150/AutoScreener`
