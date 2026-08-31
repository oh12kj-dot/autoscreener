# TENX Investment Decision v2 — 詳細実装計画

**対象リポジトリ:** `oh12kj-dot/autoscreener`
**基準ブランチ:** `main`
**基準日:** 2026-08-30
**目的:** TENXを「10バガー候補ランキング」から「実際に資金を投入する前・保有中・撤退時まで使える投資意思決定システム」へ拡張する。
**想定読者:** 実装を引き継ぐ別AIモデル、Codex、開発者。

---

## 0. この文書を読むAIへの実行指示

この文書はアイデア集ではない。**実装仕様と移行手順の引き継ぎ文書**である。以下を絶対条件として扱うこと。

1. 現在のTENXの中核である `P(horizon年でtarget_moic倍)` を、未検証の新データで直接変更しない。
2. 新規情報はまず **表示・記録・スナップショット層** に実装し、Point-in-Time履歴が十分に蓄積した後にのみモデル因子として検証する。
3. バックテスト可能性を壊さない。現在の `scoring/moic.py` が年次財務と価格履歴で閉じている設計は維持する。
4. 「データが無い」と「調べた結果該当なし」を必ず区別する。
5. LLM出力は意思決定の補助であり、売買判断や定量スコアに直接混入させない。
6. 新機能ごとに **DB → 純粋関数 → バッチ/収集 → API → UI → テスト → README** の順で実装する。
7. すべての新規データに `as_of` / `observed_at` / `source` / `coverage_status` を持たせ、将来のPoint-in-Time検証に耐えるようにする。
8. 既存ランキングを変える変更は、必ず `compare-configs` / `run-backtest` と独立検証期間で評価し、受け入れ条件を満たすまで既定値を0または無効にする。

---

# 1. 現在のTENXの理解

## 1.1 目的関数

TENXは米国の小型〜中型株について、将来の1株価値を次の恒等式へ分解し、7年後など指定ホライズンでの実現倍率を推定する。

`株主価値 ≒ 売上 × 利益率 × 評価倍率 ÷ 発行済株式数`

実装上は売上倍率、粗利率倍率、終端マルチプル変化、レバレッジ効果、希薄化を積み上げ、生存確率と不確実性を加味し、`P(MOIC >= target)` をランキングキーとしている。

## 1.2 現在すでに実装済みで、今回「不足」と扱わないもの

以下は `main` に既に存在する。新規実装担当AIは二重実装しないこと。

- `P(7年で10倍)` と任意の年数/倍率での再計算
- 期待倍率、中央値倍率、P10/P25/P50/P75/P90
- P(半値以下)、P(元本割れ)
- 生存確率、モデルσ、1年オンペース率
- 実測ボラティリティ、最大ドローダウン、現在DD、β、下落時捕捉率
- ADV、最大投入額、建玉日数、ストレス時撤退日数、推定往復コスト
- セクター内/ユニバース内分位、同業比較
- 決算サプライズ・予想改訂表示のロジック
- 経営陣、インサイダー保有、機関保有、浮動株
- EDGAR提出書類タイムライン、8-K/NT/監査人変更/上場基準等のレッドフラグ
- Form 4、空売り残、需給情報
- 将来希薄化見通し、S-3/424B5等
- 顧客集中・ガイダンス・訴訟の取得/未取得状態
- 研究ノート、プレモーテム、退出計画、検証日
- LLMによる定性要約/分析（順位には不使用）
- ポートフォリオ相関の基本関数と少なくとも1銘柄ヒット確率

主要参照ファイル:

- `src/autoscreener/scoring/moic.py`
- `src/autoscreener/scoring/portfolio.py`
- `src/autoscreener/scoring/point_in_time.py`
- `src/autoscreener/screening/price_risk.py`
- `src/autoscreener/screening/earnings_history.py`
- `src/autoscreener/screening/red_flags.py`
- `frontend/src/pages/TickerDetailPage.tsx`
- `frontend/src/dueDiligence.ts`
- `research/TEMPLATE.md`

## 1.3 現時点の最重要な制約

最新README上では、上場廃止銘柄回収等を実行した後でも最新バックテストの `delisted_settlement_rate = 0.0` で、KPIにFAILが残っている。したがって現状のバックテストは **decision-grade evidenceではない**。

この状態では、新しい因子を足すことよりも、検証基盤の回復が先である。

---

# 2. 目標アーキテクチャ

TENX v2は3層構造にする。

## Layer A — Quant Core

**完全にPoint-in-Time再構成可能で、バックテスト可能な定量コア。**

- 年次/四半期財務
- 株価・出来高
- 希薄化
- 生存/上場廃止
- 流動性
- 価格履歴から作るリスク
- 将来、十分な履歴を持った新因子のみ

原則: ここへ入る因子は「過去のその時点に同じ情報が存在した」ことを証明できなければならない。

## Layer B — Live Investment Intelligence

**現在の投資判断を改善するが、当面ランキングを変えない情報層。**

- Consensus
- Management Guidance
- Reverse Valuation
- TAM / penetration
- Operating KPI
- DEF 14Aのインセンティブ
- Debt maturity
- 会計フォレンジック
- Capital allocation
- カタリスト
- マクロ感応度

原則: 今日から毎回スナップショット保存し、将来のPoint-in-Timeデータセットを作る。

## Layer C — Portfolio Decision

**「良い銘柄」から「自分はいくら持つか」へ変換する層。**

- 現在保有
- 実測ボラ
- 相関
- セクター/テーマ集中
- 流動性
- 為替
- 税/手数料
- テーゼ破壊条件
- 売却/再検証ルール

Coreの確率を直接変更せず、ポートフォリオ側でサイズを縮小できるようにする。

---

# 3. 全体ロードマップ

| Phase | Epic | 内容 | 優先度 | Core順位を変更 | 新規データ保存 |
|---|---|---|---|---|---|
| 0 | V0 | バックテスト・生存バイアス修復 | P0 | いいえ | 一部 |
| 1 | V1 | Reverse Valuation / Expectations Gap | P0 | いいえ | 任意 |
| 1 | V2 | Consensus / Guidance Point-in-Time保存 | P0 | いいえ | はい |
| 2 | V3 | Sector Model Router | P1 | 将来 | いいえ |
| 2 | V4 | Reinvestment Quality / per-share compounding | P1 | 当面いいえ | 算出保存 |
| 2 | V5 | TAM / Market Penetration | P1 | いいえ | はい |
| 2 | V6 | Operating KPI Registry | P1 | いいえ | はい |
| 3 | V7 | Capital Allocation / Management Incentives | P1 | いいえ | はい |
| 3 | V8 | Debt Maturity / Financing Risk | P1 | いいえ | はい |
| 3 | V9 | Accounting Quality / Forensics | P1 | いいえ | 算出保存 |
| 3 | V10 | Thesis Milestones / Catalysts | P1 | いいえ | はい |
| 4 | V11 | Return Distribution Metrics | P2 | いいえ | いいえ |
| 4 | V12 | Macro / Regime Exposure | P2 | いいえ | 一部 |
| 4 | V13 | Risk-based Position Sizing | P2 | Coreはいいえ | いいえ |
| 4 | V14 | M&A competing risk | P2 | 将来 | はい |
| 4 | V15 | JPY after-tax portfolio return | P2 | いいえ | いいえ |

---

# 4. EPIC V0 — バックテストと生存バイアスの完全修復

## 4.1 目的

他のすべてのモデル変更を評価できる状態へ戻す。ここが未完のまま、新因子の採否をバックテストKPIで決めてはいけない。

## 4.2 必須受け入れ条件

- `delisted_settlement_rate > 0`
- 上場廃止銘柄が評価母集団へ含まれ、廃止後価格欠損を「生存」と扱わない
- M&A/現金決済、破綻、他市場移行を最低限別分類する
- 同一ホライズンの評価期間が重なる問題をメトリクスで明示する
- 独立評価期間またはblock bootstrapのCIを出す
- `/validation` にPASS/FAIL理由を機械可読で表示
- FAIL中はランキング画面上部に「Research Only / validation failed」を固定表示

## 4.3 データモデル

既存の上場廃止テーブルを確認し、不足する場合のみ以下を追加する。

### `delisting_events`

- `ticker_id`
- `event_date`
- `event_type`: `bankruptcy | cash_acquisition | stock_acquisition | exchange_transfer | liquidation | unknown`
- `last_trade_date`
- `last_trade_price`
- `settlement_value_per_share`
- `settlement_date`
- `source`
- `source_url`
- `observed_at`
- `confidence`

### バックテスト結果へ追加

- `delisted_count`
- `delisted_settled_count`
- `delisted_settlement_rate`
- `bankruptcy_count`
- `mna_count`
- `unknown_delisting_count`
- `effective_independent_periods`

## 4.4 ロジック

1. 評価日に存在したユニバースを固定する。
2. 将来ホライズン末までに上場廃止した場合、最後の価格を単純forward-fillしない。
3. 現金買収なら現金対価をtotal returnへ含める。
4. 株式交換は取得企業株価へロールできる場合のみ計算し、できなければunknown。
5. 破綻/清算で回収額不明なら0とunknownの両シナリオを出し、主KPIは保守側を採用。
6. データ不能銘柄を母集団から削除しない。

## 4.5 テスト

- 破綻して0になる銘柄
- $12の現金TOB
- 1:0.5株式交換
- 廃止日直前に株価欠損
- symbol reuse
- delisting event source missing
- overlapping evaluation dates

---

# 5. EPIC V1 — Reverse Valuation / Expectations Gap

## 5.1 目的

「この会社は伸びそう」ではなく、**現在価格が何を既に織り込んでいるか**を示す。

これはTENX v2の最重要新機能である。

## 5.2 新規モジュール

`src/autoscreener/scoring/reverse_valuation.py`

### 主要関数

```python
@dataclass(frozen=True)
class ReverseValuationScenario:
    required_return: float
    implied_revenue_cagr: float | None
    implied_terminal_margin: float | None
    implied_terminal_multiple: float | None
    feasible: bool
    reason: str | None


def solve_implied_growth(inputs, config, required_return, *, terminal_margin=None, terminal_multiple=None): ...
```

## 5.3 計算方針

現在株価/時価総額を起点に、投資家が要求する将来リターン `r` を満たすために必要な企業側の成長を逆算する。

基本式:

`required_terminal_equity = current_market_cap × (1+r)^H`

`terminal_equity = terminal_EV - terminal_net_debt`

`terminal_EV = terminal_gross_profit × terminal_EV_GP`

`terminal_gross_profit = current_revenue × revenue_growth_path × terminal_margin`

未知数を売上CAGRとし、既存の成長フェード、希薄化、買い戻し減衰、終端マルチプル上限などは **Coreと同じ設定関数を再利用**する。

### 表示する要求収益率

既定:

- 10%
- 15%
- 20%
- 25%
- 30%

設定化する。

## 5.4 Expectations Gap

追加表示:

- `TENX implied initial growth`
- `market-implied growth @ 15/20/25% required return`
- `consensus next-1y / next-2y growth`
- `management guidance`
- 差分 `TENX - market_implied`
- 差分 `consensus - market_implied`

### 注意

この差を新しい総合スコアへ足さない。まず表示のみ。

## 5.5 API

`GET /api/v1/candidates/{ticker}/reverse-valuation?horizon_years=7`

レスポンス例:

```json
{
  "ticker": "ABCD",
  "as_of": "2026-08-30",
  "horizon_years": 7,
  "scenarios": [
    {"required_return": 0.15, "implied_revenue_cagr": 0.21},
    {"required_return": 0.20, "implied_revenue_cagr": 0.29},
    {"required_return": 0.25, "implied_revenue_cagr": 0.36}
  ]
}
```

## 5.6 UI

Ticker detailに **「市場が織り込む成長」** セクションを追加。

テーブル列:

- 要求収益率
- 現在価格が要求する売上CAGR
- TENX初期成長率との差
- Consensusとの差
- Management Guidanceとの差

グラフは横軸 required return、縦軸 implied CAGR の1本線で十分。

## 5.7 テスト

- growth=0で成立
- 高すぎる価格で解なし
- net cash企業
- high leverage企業
- dilutionあり
- buybackあり
- terminal cap発動
- 任意ホライズン

---

# 6. EPIC V2 — Consensus / Guidance Point-in-Time Data Store

## 6.1 目的

現在値を表示するだけでなく、**今日から履歴を蓄積し、将来バックテストできる状態を作る。**

## 6.2 テーブル

### `analyst_consensus_snapshots`

- `ticker_id`
- `observed_at`
- `source`
- `period_type`: FY/Q
- `period_end`
- `revenue_mean`
- `revenue_low`
- `revenue_high`
- `eps_mean`
- `ebitda_mean`
- `analyst_count`
- `target_price_mean`（参考。Coreには使用しない）
- `raw_payload`
- unique: `(ticker_id, observed_at, period_end, source)`

### `management_guidance_snapshots`

既存guidanceを履歴型へ拡張できるなら新規テーブル不要。最低限:

- `announced_at`
- `period_end`
- `metric`
- `low`
- `high`
- `unit`
- `source_filing_accession`
- `source_url`
- `status`: initiated/raised/lowered/reaffirmed/withdrawn

## 6.3 収集

可能なデータソースを抽象化する。

```python
class ConsensusProvider(Protocol):
    def fetch(self, ticker: str, as_of: datetime) -> ConsensusSnapshot: ...
```

最初のproviderがyfinanceであっても、DBスキーマはprovider非依存にする。

## 6.4 重要な原則

- `observed_at` を必ず保存
- 既存行を上書きしない
- 変更がない日も保存するかは設定化。推奨は「内容ハッシュが変わった時だけ保存」
- Coreモデルへ入れない

## 6.5 派生指標

- revenue estimate revision 7d / 30d / 90d
- EPS revision breadth
- analyst count change
- guidance vs consensus gap
- reported vs prior consensus surprise

---

# 7. EPIC V3 — Sector Model Router

## 7.1 目的

一般事業会社、銀行、保険、REIT、資源、バイオ等を一つの経済モデルで評価しない。

## 7.2 設計

新規:

`src/autoscreener/scoring/model_router.py`

```python
class ValuationModel(Protocol):
    name: str
    version: str
    def supports(profile) -> bool: ...
    def build_inputs(...): ...
    def score(...): ...
```

### 初期カテゴリ

1. `general_corporate` — 現行MOICモデル
2. `bank`
3. `insurance`
4. `reit`
5. `commodity_producer`
6. `biotech_pre_revenue`
7. `unclassified`

## 7.3 判定

sectorだけでなくindustry、財務科目、収益構造を使う。

例:

- Banks / Financial Services → bank
- Insurance → insurance
- REIT業種 + FFO/AFFO取得 → reit
- Gold/Copper/Oil producer → commodity
- revenueほぼ0 + R&D高 + biotech industry → biotech_pre_revenue

## 7.4 最初の出荷方針

**ルータだけ実装し、general以外は「専用モデル未提供」表示としてランキングから外す設定を用意する。**

いきなり全専用モデルを作らない。

### Phase 2A

- `model_family` を表示
- 現行ランキングのsector偏りをmodel_family別に検証

### Phase 2B

commodity専用モデルから実装する。

### Commodity model例

- production volume
- realized commodity price
- unit cash cost / AISC
- reserve life
- capex
- net debt
- commodity price scenario

### Bank model例

- tangible book value
- ROTCE
- NIM
- credit cost
- CET1
- P/TBV terminal multiple

## 7.5 受け入れ条件

- 現行一般企業について1bitも結果を変えない
- router無効時に完全互換
- model_familyごとのバックテストを別表示

---

# 8. EPIC V4 — Reinvestment Quality / Per-share Compounding

## 8.1 目的

売上成長ではなく、**追加資本を高い収益率で再投資できるか**を測る。

## 8.2 新規指標

- `incremental_roic_3y`
- `incremental_roic_5y`
- `reinvestment_rate`
- `nopat_growth`
- `gross_profit_per_share_cagr_3y`
- `fcf_per_share_cagr_3y`
- `revenue_per_share_cagr_3y`
- `organic_growth_proxy`
- `acquisition_intensity`

## 8.3 計算

### Invested Capital

`IC = operating_assets - operating_liabilities`

簡易版:

`IC ≈ equity + interest_bearing_debt - excess_cash`

### NOPAT

`NOPAT = EBIT × (1 - normalized_tax_rate)`

税率は0〜35%へclipし、赤字時はNULL。

### Incremental ROIC

`ΔNOPAT / ΔInvestedCapital`

単年ノイズが大きいため3年/5年差分を優先。

### Reinvestment Rate

`ΔInvestedCapital / NOPAT`

またはFCFから推定する。複数定義を同時に出し、無理に1値へ統合しない。

## 8.4 per-shareを必須にする理由

企業全体の売上が増えても株式数が増えれば株主の複利は薄まる。

そのため企業成長と必ず並べて:

- revenue CAGR
- revenue/share CAGR
- gross profit CAGR
- gross profit/share CAGR
- FCF CAGR
- FCF/share CAGR

を表示する。

## 8.5 UI

「複利の質」セクション:

| 指標 | 3年 | 5年 | ユニバース分位 | セクター分位 |
|---|---:|---:|---:|---:|
| Incremental ROIC | | | | |
| Revenue/share CAGR | | | | |
| GP/share CAGR | | | | |
| FCF/share CAGR | | | | |
| Reinvestment rate | | | | |

## 8.6 Coreへの採用

当面表示のみ。最低2〜3年のpoint-in-time履歴ができた後、rank ICと右裾イベントliftを評価する。

---

# 9. EPIC V5 — TAM / Market Penetration

## 9.1 目的

高成長が数学的に持続可能かを判断する。会社発表の巨大TAMをそのままスコアにしない。

## 9.2 テーブル

### `market_opportunity_estimates`

- `ticker_id`
- `as_of`
- `method`: company_reported / bottom_up / third_party / manual
- `tam_value`
- `sam_value`
- `current_revenue_addressable`
- `penetration_rate`
- `currency`
- `formula_text`
- `source_url`
- `source_excerpt`
- `confidence`: low/medium/high
- `created_by`: machine/manual/llm-assisted

## 9.3 Bottom-up式

自由文字列だけでなく構成要素を持てるようにする。

### `market_opportunity_components`

- estimate_id
- `component_name`
- `quantity`
- `unit`
- `price_per_unit`
- `penetration_assumption`
- `result_value`

## 9.4 LLM利用

LLMは10-K/IR資料から候補式を抽出してよいが、必ず:

- 引用
- 元URL
- 数字の所在
- 「会社発表」「AI推定」「ユーザー入力」の区分

を保存する。

## 9.5 UI

「成長余地」:

- Current revenue
- TAM
- SAM
- Penetration
- TENXの7年後売上
- 7年後売上/TAM

**7年後売上がTAMを超える場合は赤い警告ではなく「assumption conflict」表示。**

---

# 10. EPIC V6 — Operating KPI Registry

## 10.1 目的

財務諸表より先に動く事業KPIを保存する。

## 10.2 汎用スキーマ

### `operating_kpi_definitions`

- `id`
- `code`
- `label`
- `unit`
- `sector/model_family`
- `higher_is_better`
- `description`

### `operating_kpi_observations`

- `ticker_id`
- `kpi_definition_id`
- `period_end`
- `reported_at`
- `value`
- `source_accession`
- `source_url`
- `source_excerpt`
- `extraction_method`: regex/xbrl/llm/manual
- `confidence`

## 10.3 初期KPI候補

### SaaS

ARR, NRR, RPO, billings, churn, customer count, ACV

### Consumer

same-store sales, stores, ARPU, active users

### Semiconductor

units, ASP, utilization, backlog

### Industrial

book-to-bill, backlog, capacity

### Biotech

trial phase, enrollment, readout date, cash runway

### Commodity

production, AISC/cash cost, reserves, realized price

## 10.4 抽出

XBRLで取れるものはXBRL最優先。次に定型表regex。LLMは最後。

同じKPI名でも会社ごとに定義が違うため、**会社定義文を必ず保存する。**

---

# 11. EPIC V7 — Capital Allocation / Management Incentives

## 11.1 Capital Allocation Events

### テーブル `capital_allocation_events`

- ticker_id
- announced_at
- event_type: acquisition/divestiture/buyback/equity_raise/debt_raise/capex/dividend
- amount
- currency
- counterparty_or_asset
- shares_issued
- price_per_share
- source_accession
- source_url

### 派生指標

- 3y acquisition spend / market cap
- 3y equity issuance / market cap
- buyback yield
- share issuance price percentile vs own history
- acquisition goodwill growth
- post-acquisition ROIC change

## 11.2 Management Incentives

### `management_incentive_snapshots`

- ticker_id
- proxy_date
- executive_name
- role
- founder_flag
- tenure_years
- beneficial_ownership_pct
- total_compensation
- equity_compensation_pct
- performance_metrics JSON
- change_in_control_terms
- source_accession

## 11.3 UI

「経営陣と資本配分」:

- Founder-led
- CEO tenure
- CEO ownership
- 過去3年増資額
- 過去3年買戻し額
- M&A額
- SBC / revenue
- 報酬KPI

点数化はしない。事実を並べる。

---

# 12. EPIC V8 — Debt Maturity / Financing Risk

## 12.1 目的

Net Debtでは見えない「いつ返済が必要か」「借換可能か」を出す。

## 12.2 テーブル

### `debt_instruments`

- ticker_id
- instrument_id
- as_of
- instrument_type
- principal
- currency
- coupon_rate
- rate_type: fixed/floating
- benchmark_rate
- maturity_date
- secured_flag
- convertible_flag
- conversion_price
- covenant_summary
- source_accession

### `liquidity_facilities`

- revolver_total
- revolver_drawn
- revolver_available
- atm_remaining
- shelf_remaining
- cash_balance

## 12.3 派生

- debt_due_12m / cash
- debt_due_24m / cash
- debt_due_36m / EBITDA
- weighted_average_interest_rate
- floating_debt_pct
- liquidity_coverage_months
- refinancing_wall_year

## 12.4 UI

Debt maturity bar table:

2027: $20M
2028: $45M
2029: $310M ← major wall

合わせてcash/revolver availabilityを表示。

## 12.5 テーゼ連携

`debt_due_12m > cash + undrawn_revolver` なら自動売却ではなく `financing_review_required` を点灯。

---

# 13. EPIC V9 — Accounting Quality / Forensics

## 13.1 指標

- accrual ratio
- CFO / net income
- CFO / EBITDA
- receivables growth - revenue growth
- inventory growth - revenue growth
- deferred revenue trend
- capitalized software / R&D
- SBC / revenue
- goodwill / assets
- acquisition intangible growth
- non-GAAP adjustment / GAAP operating income
- related party disclosure flag
- auditor tenure/change
- restatement history

## 13.2 Pure functions

`src/autoscreener/screening/accounting_quality.py`

```python
@dataclass(frozen=True)
class AccountingQuality:
    accrual_ratio: float | None
    cash_conversion: float | None
    receivables_gap: float | None
    inventory_gap: float | None
    sbc_to_revenue: float | None
    goodwill_to_assets: float | None
    warnings: list[str]
```

## 13.3 表示

「利益の質」セクションに3年推移を置く。

警告条件は閾値を固定しすぎず、**ユニバース/セクター分位 + 自社履歴変化**を併記する。

---

# 14. EPIC V10 — Thesis Milestones / Catalysts

## 14.1 目的

「次回決算日」ではなく、投資仮説がいつ何によって検証されるかを管理する。

## 14.2 テーブル

### `thesis_milestones`

- id
- ticker_id
- created_at
- due_date
- category: financial/product/customer/regulatory/capacity/financing/other
- metric_code
- bull_threshold
- base_threshold
- bear_threshold
- unit
- source: user/model/company
- status: pending/met/missed/unknown
- resolved_at
- actual_value
- note

## 14.3 研究ノート統合

`research/TEMPLATE.md` に `milestones` を追加。

```yaml
milestones:
  - due_date: 2026-11-05
    metric: revenue_yoy
    bull: 0.35
    base: 0.25
    bear: 0.15
  - due_date: 2027-03-31
    metric: customer_count
    base: 5000
```

## 14.4 UI

保有画面に:

- 期限まで残り日数
- 最新実績
- bull/base/bearのどこに着地したか
- 連続miss数

を出す。

---

# 15. EPIC V11 — Return Distribution Metrics

## 15.1 目的

`P(10x)` だけでなく「普通に良い投資」である確率も表示する。

## 15.2 追加計算

既存の対数正規/生存混合分布から:

- `P(CAGR >= 10%)`
- `P(CAGR >= 15%)`
- `P(CAGR >= 20%)`
- `P(CAGR >= 25%)`
- `P(MOIC >= 2x)`
- `P(MOIC >= 3x)`
- `expected_cagr`
- `median_cagr`
- `expected_shortfall_10pct`（モデル仮定値であることを明示）

## 15.3 注意

現在のσは強く縮小されているため、分布指標を銘柄固有の精密リスクだと表現しない。

UIに「モデル仮定ベース」と常時表示する。

---

# 16. EPIC V12 — Macro / Regime Exposure

## 16.1 目的

資源、金融、景気敏感株について、企業固有成長とマクロ追い風を分離する。

## 16.2 マクロ系列

- Fed Funds / SOFR
- 2Y / 10Y Treasury
- HY spread
- USD index
- WTI
- Gold
- Copper
- Natural Gas
- Housing starts
- PMI等

## 16.3 Point-in-Time

改定される系列はvintageを保存する。`observed_at` と `observation_date` を分離する。

## 16.4 派生

- rolling beta to macro factors
- downside beta
- revenue sensitivity（長期履歴がある場合）
- margin sensitivity

## 16.5 UI

「マクロ感応度」:

- Oil beta +0.65
- Copper beta +0.42
- Rates beta -0.30

と「統計的な関連で因果ではない」と注記。

---

# 17. EPIC V13 — Risk-based Position Sizing

## 17.1 現状

`config/portfolio.yaml` では1銘柄4%上限、二値イベント2%、ADV制約等がある。

これをhard capとして維持する。

## 17.2 新しい推奨サイズ

Core probabilityをケリー式へ入れない。確率の絶対較正がまだ弱いため。

推奨サイズは **最大許容量を縮小する方向のみ**。

```text
base_cap = min(per_position_cap, liquidity_cap)
vol_factor = min(1, target_vol / realized_vol)
correlation_factor = f(portfolio_cluster_exposure)
sector_factor = f(sector_cap_remaining)
uncertainty_factor = f(evidence_grade)
recommended_cap = base_cap × vol_factor × correlation_factor × sector_factor × uncertainty_factor
```

## 17.3 例

4% hard capでも:

- vol 120% → 0.55倍
- 同テーマ集中 → 0.7倍
- Evidence C → 0.75倍

なら1.15%程度まで縮小。

## 17.4 config

`portfolio.yaml`:

```yaml
risk_sizing:
  enabled: false
  target_annual_vol: 0.60
  min_vol_factor: 0.35
  max_pairwise_corr_soft: 0.65
  evidence_grade_factors:
    A: 1.0
    B: 0.9
    C: 0.75
    D: 0.5
```

最初は `enabled: false`。

---

# 18. EPIC V14 — M&A / Competing Risk

## 18.1 問題

小型株は10倍になる前に現金買収されることがある。これは「倒産」と違い、上振れを途中で打ち切るイベント。

## 18.2 モデル化

長期的には:

- bankruptcy hazard
- acquisition hazard
- survive independent

のcompeting risksへ分ける。

## 18.3 初期実装

まずモデルへ入れず、過去のM&A頻度をmodel_family / market_cap / valuation別に集計し表示。

`P(acquisition within 3y)` は履歴が十分な場合のみ。

---

# 19. EPIC V15 — 日本人投資家向けJPY・税引後リターン層

## 19.1 原則

企業評価モデルと分離する。企業そのものの価値と、ユーザーの税/為替事情を混ぜない。

## 19.2 入力設定

- account_type: taxable/NISA
- tax_rate_capital_gain
- tax_rate_dividend
- fx_spread_bps
- brokerage_fee_bps
- base_currency: JPY

## 19.3 出力

- USD pre-tax MOIC
- JPY pre-tax MOIC
- JPY after-tax MOIC
- annualized IRR
- break-even USDJPY

為替は予測せず、複数シナリオで表示する。

---

# 20. 共通データ設計ルール

## 20.1 すべてのLive Intelligenceテーブルに必要な列

- `ticker_id`
- `observed_at`
- `source`
- `source_url` または `source_accession`
- `raw_payload` または原文参照
- `coverage_status`
- `confidence`

## 20.2 `coverage_status`

最低:

- `not_collected`
- `collected_no_finding`
- `collected_with_data`
- `collection_failed`
- `not_applicable`

## 20.3 NULLポリシー

欠損を0にしない。

特に:

- analyst_count不明 ≠ 0人
- debtなし ≠ debt data missing
- customer concentrationなし ≠ 未収集
- lawsuitなし ≠ 未収集
- TAM不明 ≠ TAM=0

---

# 21. API設計

## 21.1 原則

`CandidateDetail` を無限に肥大化させない。大きい新セクションは独立endpointへ分離し、TickerDetailPageは並列fetchする。

## 21.2 推奨endpoint

- `GET /api/v1/candidates/{ticker}/reverse-valuation`
- `GET /api/v1/candidates/{ticker}/consensus`
- `GET /api/v1/candidates/{ticker}/reinvestment-quality`
- `GET /api/v1/candidates/{ticker}/market-opportunity`
- `GET /api/v1/candidates/{ticker}/operating-kpis`
- `GET /api/v1/candidates/{ticker}/capital-allocation`
- `GET /api/v1/candidates/{ticker}/management-incentives`
- `GET /api/v1/candidates/{ticker}/debt-profile`
- `GET /api/v1/candidates/{ticker}/accounting-quality`
- `GET /api/v1/candidates/{ticker}/thesis-milestones`
- `GET /api/v1/candidates/{ticker}/macro-exposure`
- `GET /api/v1/positions/risk-sizing`

## 21.3 APIレスポンス共通メタ

```json
{
  "ticker": "ABCD",
  "as_of": "2026-08-30",
  "coverage_status": "collected_with_data",
  "source": "sec_edgar",
  "data_age_days": 2,
  "data": {}
}
```

---

# 22. Frontend情報設計

## 22.1 Ticker Detailの新しい読み順

現在は情報量が多いため、順序を意思決定順へ変える。

1. **Decision Header**
   - P(10x)
   - expected/median MOIC
   - 1年オンペース
   - Validation status
   - Evidence grade
2. **Expectations Gap**
   - Market implied vs TENX vs Consensus vs Guidance
3. **Business Quality**
   - per-share growth / reinvestment / operating KPIs
4. **Valuation & Peer Context**
5. **Downside / Financing**
   - debt maturity / dilution / accounting quality / red flags
6. **Management / Capital Allocation**
7. **TAM / Growth runway**
8. **Catalysts / Milestones**
9. **Liquidity / Execution**
10. **Filings / Sources**
11. **Research note / LLM analysis**

## 22.2 Decision Headerに必ず出す状態

- `VALIDATION PASS / FAIL / STALE`
- `DATA COVERAGE A-D`
- `MODEL FAMILY`
- `LIVE INTELLIGENCE age`

## 22.3 UX原則

- 「未取得」を灰色
- 「取得済み該当なし」を通常文字
- 「異常」を警告色
- スコアに入っていない情報には `Not used in ranking` バッジ
- 人間入力値には `Manual` バッジ
- LLM抽出には `AI extracted — verify source` バッジ

---

# 23. バッチ・ジョブ設計

## 23.1 日次

- prices
- current consensus snapshots
- event calendar
- insider/short interest（取得頻度に応じる）
- ranking/scoring
- validation freshness

## 23.2 提出書類イベント駆動

新規10-K/10-Q/8-K/DEF14A検知時:

1. filing sections抽出
2. guidance
3. debt instruments
4. capital allocation events
5. management incentives（DEF14A）
6. operating KPI
7. customer concentration
8. litigation
9. accounting flags

## 23.3 月次/四半期

- TAM再確認
- macro exposure recalculation
- peer set refresh
- model-family classification refresh

---

# 24. テスト戦略

## 24.1 Unit tests

各計算ロジックはDBから切り離した純粋関数にする。

必須:

- normal case
- missing data
- negative values
- extreme values
- currency mismatch
- split-adjustment impact
- duplicated observations
- stale data
- source conflict

## 24.2 Point-in-Time tests

すべてのsnapshot系で:

「2026-06-30 as_ofでqueryした時、2026-07-01以降に観測された値が出ない」

ことを固定テストする。

## 24.3 Regression tests

Live Intelligence追加後:

`same config + same score_date => all probability exactly identical`

をCIへ追加。

## 24.4 Model promotion tests

新因子をCoreへ入れる場合のみ:

- rank IC
- decile monotonicity
- top-tail lift
- bankruptcy/failure rate
- calibration error
- turnover
- sector concentration
- confidence interval

を既存モデルと比較する。

改善が1KPIだけでは採用しない。

---

# 25. Observability / Data Quality

## 25.1 新しい管理画面

`/data-coverage`

テーブル:

| Dataset | coverage | stale | failed | last successful | source |
|---|---:|---:|---:|---|---|
| Consensus | 72% | 5% | 3% | ... | ... |
| Guidance | ... | | | | |
| Debt | | | | | |
| KPI | | | | | |

## 25.2 モデル停止条件

以下の場合、スコア計算自体を止めるのではなく **validation bannerをFAIL** にする。

- delisted settlement rateが閾値未満
- benchmark history不足
- score universeが急減
- critical source stale
- config hashとcalibration map不一致

---

# 26. 実装順序

## Sprint 0 — Validation Recovery

1. delisting settlementの原因調査
2. settlement event型実装
3. backtest修正
4. validation banner
5. regression

**これが完了するまでCoreモデルの新因子検討は禁止。**

## Sprint 1 — Expectations

1. Reverse valuation pure function
2. API
3. UI
4. consensus snapshot schema
5. collector
6. guidance/consensus comparison

## Sprint 2 — Business Quality

1. per-share growth
2. incremental ROIC
3. reinvestment
4. operating KPI generic schema
5. initial extractors

## Sprint 3 — Downside & Management

1. debt maturity
2. accounting quality
3. capital allocation events
4. DEF14A management incentives

## Sprint 4 — Research Workflow

1. TAM schema
2. thesis milestones
3. catalyst UI
4. research note template update

## Sprint 5 — Portfolio

1. risk sizing preview
2. macro exposure
3. JPY after-tax scenarios
4. M&A history layer

## Sprint 6 — Model Research

蓄積されたLive Intelligenceから、十分なPoint-in-Time履歴があるものだけCore候補として評価。

---

# 27. Definition of Done

各Epicは以下を満たしたら完了。

- DB migrationがある場合、upgrade/downgradeが通る
- Pure functionにunit test
- collectorが再実行可能（idempotent）
- sourceとobserved_atが保存される
- API schema型定義
- frontend build成功
- `not_collected` と `no finding` が区別される
- READMEへ収集手順
- pipeline healthへ追加
- Core probabilityが変更されないことをregressionで確認
- UIにデータ時点を表示
- 原本リンクがある

---

# 28. AI実装担当向けタスク分割

以下の順序でPRを小さく切る。

## PR-001 Validation hard gate

- backtest settlement修正
- validation status API
- ranking/detail banner

## PR-002 Reverse valuation engine

- pure function only
- unit tests

## PR-003 Reverse valuation API/UI

- endpoint
- TypeScript types
- section UI

## PR-004 Consensus snapshots

- migration
- provider interface
- collector
- coverage metrics

## PR-005 Expectations comparison

- TENX vs market-implied vs consensus vs guidance

## PR-006 Reinvestment metrics

- calculation
- API
- UI

## PR-007 Operating KPI registry

- generic schema
- 2〜3種類のKPI extractor

## PR-008 Debt profile

- instruments
- maturity ladder
- liquidity coverage

## PR-009 Accounting quality

- ratios
- anomaly display

## PR-010 Capital allocation / DEF14A

- events
- incentives

## PR-011 TAM / milestones

- market opportunity
- research note integration

## PR-012 Portfolio sizing preview

- risk shrink factors
- default OFF

## PR-013 Sector router

- classification only
- no scoring change

その後、model-familyごとの専用モデルを独立PRで実装する。

---

# 29. 現行コードとの接続ポイント

## Backend

- `src/autoscreener/scoring/moic.py` — Coreを壊さない
- `src/autoscreener/scoring/point_in_time.py` — 過去時点再構成
- `src/autoscreener/scoring/portfolio.py` — portfolio layer拡張
- `src/autoscreener/api/routes.py` — 新endpoint
- `src/autoscreener/api/schemas.py` — schema
- `src/autoscreener/db/models.py` — migration反映
- `src/autoscreener/batch/daily_pipeline.py` — 日次収集
- `src/autoscreener/collectors/edgar_client.py` — SEC入力

## Frontend

- `frontend/src/pages/TickerDetailPage.tsx`
- `frontend/src/pages/PositionsPage.tsx`
- `frontend/src/pages/ValidationPage.tsx`
- `frontend/src/pages/PipelinePage.tsx`
- `frontend/src/api/client.ts`
- `frontend/src/api/types.ts`
- `frontend/src/dueDiligence.ts`

新しい大規模セクションは `frontend/src/components/` に分離する。

---

# 30. 実装してはいけないこと

1. Consensusを今日の値だけ取得し、過去バックテストへ後付けする。
2. TAMをLLMに自由生成させ、それを点数化する。
3. CEOのインサイダー保有率だけで「良い経営者」と判定する。
4. debt data missingをdebt=0として扱う。
5. P(10x)とLLM convictionを加重平均する。
6. Current priceが高いという理由だけでモデルへ機械的value penaltyを足す。
7. Sector capをCore scoreへ混ぜる。
8. モデルFAIL中にUIからvalidation warningを隠す。
9. Liveデータを上書き保存し、過去時点を失う。
10. 既存の `probability` を新UI実装のついでに変更する。

---

# 31. 最終的な完成画面のイメージ

## Decision Header

**ABCD — P(7y 10x) 3.2% / Expected MOIC 4.1x / Validation PASS / Evidence B**

### Expectations Gap

| | Growth |
|---|---:|
| Market implied @20% return | 23% |
| Consensus | 27% |
| Management guidance | 25–29% |
| TENX | 32% |

「TENXは市場織込より+9pt強気」

### Business Quality

- Incremental ROIC 28%
- GP/share CAGR 31%
- FCF/share CAGR 18%
- NRR 118%
- RPO +42%

### Growth Runway

- Current revenue $420M
- Bottom-up TAM $8.2B
- penetration 5.1%
- TENX 7y revenue $2.6B = TAM 32%

### Downside

- Cash $180M
- Debt $240M
- 2028 maturity $190M
- runway 8 quarters
- P(loss) 31%

### Management

- Founder CEO ownership 12%
- 3y equity issuance $20M
- buyback $0
- acquisitions $110M
- SBC/revenue 3.1%

### Milestones

- Nov earnings: revenue YoY base >=25%
- Q1: gross margin >=48%
- 2027: customer count >=5,000

### Position

- Hard cap 4%
- Liquidity cap 3.4%
- Risk-adjusted preview 1.8%
- Existing correlated exposure 6.2%

この形なら、ランキングの数字だけで買うのではなく、**何を信じ、何が既に価格へ織り込まれ、何が外れたら撤退するか**まで一画面で把握できる。

---

# 32. 最終優先順位

最重要順:

1. **バックテストのdecision-grade化**
2. **Reverse Valuation / Expectations Gap**
3. **Consensus / GuidanceのPoint-in-Time保存開始**
4. **Reinvestment Quality + per-share compounding**
5. **Sector Model Router**
6. **Operating KPI**
7. **Debt maturity / refinancing risk**
8. **Accounting quality**
9. **Capital allocation / management incentives**
10. **TAM / penetration**
11. **Thesis milestones**
12. **Risk-based sizing**
13. **Macro exposure**
14. **M&A competing risk**
15. **JPY after-tax layer**

この順番を守る理由は、**まずモデルを信頼できるようにし、その次に市場との期待差を測り、その後で事業の質と下方リスクを増やし、最後にポートフォリオ最適化へ進む**ためである。

---

# 33. 引き継ぎ時の最初のチェックリスト

実装AIは最初に次を確認する。

- [ ] 最新 `main` をpull
- [ ] READMEのoperation recordを確認
- [ ] `/validation` の最新FAIL理由を確認
- [ ] 最新BacktestRunの `delisted_settlement_rate`
- [ ] `config/scoring.yaml` の既定値とmodel version
- [ ] `config/portfolio.yaml`
- [ ] `alembic/versions/` の最新head
- [ ] `frontend/src/pages/TickerDetailPage.tsx` の現行セクション
- [ ] `CandidateDetail` 型とAPI schema
- [ ] 既存テスト全通過
- [ ] 同一score_dateでprobability regression snapshotを作成

ここまで確認してからPR-001へ着手する。
