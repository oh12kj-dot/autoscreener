# 投資判断に必要な情報の棚卸しと、不足分の実装計画(2026-08-29)

この文書は「TENX のランキングを見てから実際に買い、持ち、売るまで」に必要な情報を
**判断の段階ごとに列挙**し、各項目が現在のアプリで満たされているかを実装を読んで確認し、
**満たされていないものについて実装計画を書いた**ものである。

判断の原則は 30.1.1(`outside_tenx_implementation_plan_2026-08-28.md`)をそのまま踏襲する。

- **原則1**:機械が測れるものだけ機械に測らせる。定性判断は代行せず、記録の器と外部への導線を用意する。
- **原則2**:利用者が書くデータはファイル(git)、機械が導くデータはDB。
- **原則3**:EDGAR 由来・需給由来のシグナルを `evaluate_gates` とスコアリングに入れない(検証資産を壊さないため)。

追加で、この文書は次の原則を1つ足す。

- **原則4**:**既にDBにある情報を画面に出していない箇所は、新規データ収集より先に直す。**
  収集を増やすとレート制限・ポイントインタイム汚染・保守対象が増えるが、既存データの提示にはそれが無い。

---

## 1. 結論(先に3つ)

### ① 最大の不足は機能ではなく、モデルの検証状態である

`/validation` と直近の `run-backtest` のとおり、**lift / calibration / loss の KPI はすべて FAIL**、
v4 はモメンタム・売上成長のベースラインをほとんど上回っていない。原因の本丸は残課題 R-1
(バックテストの生存バイアス)で、これを直すための実装(`collectors/delisting_source.py`・
`cli collect-delistings`)は**コードとしては完成しているが、一度も実行されていない**。
加えて `cli recover-quarantine` が未実行のまま 5,312 銘柄が隔離状態にある。

**この2つを走らせて `run-backtest` をやり直すまで、ランキングの順位は投資判断の入力として使えない。**
どんな新機能もこの前に来ることはない(→ J-0)。

### ② 機能面の最大の穴は「会社の姿が画面に無い」こと

現在の銘柄詳細に出るのは、5因子の内訳・診断値・確率の推移・取扱可否・希薄化・レッドフラグである。
**「この会社が何を売っていくら稼いでいるのか」は1文字も出ない。**
事業概要(`info.longBusinessSummary`)は **`raw_snapshots` の 16,108 行に既に入っている**のに
API にもUIにも出ていない(未取得は 185 行のみ)。財務三表も `payload` に丸ごと入っているが、
モデルの入力として消費されるだけで、売上・粗利率・現金・株式数の**推移そのものは表示されない**。

今のUIは「順位は見えるが、順位の対象が何なのかは見えない」状態であり、原則1が防ごうとした
「読んだつもり」とは逆に、**見ないまま判断する**方向に効いてしまっている(→ J-1〜J-4)。

### ③ 判断の後工程(執行タイミング・需給・売却)がほぼ空白

取扱可否・ADV上限・往復コストまでは実装済みだが、**いつ建てるか(カタリスト)**、
**誰が売っているか(需給)**、**どうなったら降りるか(売却規律)**の3つは器すら無い。
30.9.2 のデューデリ・チェックリストと 30.9.3 の免責表示も、計画にありながら未実装である
(フロントエンドに該当コードなし)(→ J-5〜J-9)。

---

## 2. 投資判断に必要な情報の全体マップ

判断を8段階に分け、各段階で必要な情報を列挙する。
状態は ✅=実装済み / △=部分的 / ❌=無い。

### 段階A. 母集団と足切り(そもそも検討対象か)

| # | 必要な情報 | なぜ必要か | 状態 | 実装 |
|---|---|---|---|---|
| A1 | 時価総額・売上高の上限/下限 | 算術上10倍になれる規模か | ✅ | `config/universe.yaml`(目標倍率に連動、29章) |
| A2 | 株価下限・流動性下限 | 執行不能・上場基準割れの排除 | ✅ | `screening/exclusion_gates.py` |
| A3 | セクター除外 | 恒等式モデルが成立しない業種 | ✅ | 同上(`excluded_sector`) |
| A4 | 債務超過・キャッシュランウェイ | 7年生存の最低条件 | ✅ | `negative_equity` / `cash_runway_floor` |
| A5 | 希薄化率の上限 | 発行済株式数の増加で分母が壊れる | ✅ | `dilution_ceiling` |
| A6 | 上場からの経過期間 | 外挿に足る観測期間 | ✅ | `insufficient_listing_history` |
| A7 | 除外理由の可視化 | 「なぜ出てこないのか」の説明責任 | ✅ | `/excluded`・`/watchlist` |

**この段階に不足は無い。**

### 段階B. 事業の理解(何をしている会社か)

| # | 必要な情報 | なぜ必要か | 状態 | ギャップ |
|---|---|---|---|---|
| B1 | 事業概要(何を誰に売るか) | 定量シグナルの意味を決める前提 | ❌ | **J-1** |
| B2 | 業種・所在国・従業員数・IRサイト | 一次情報への入口 | △ | 業種のみ。**J-1** |
| B3 | 上場時期・上場市場 | 履歴の長さと開示水準 | △ | `tickers.listed_date` はあるが未表示。**J-1** |
| B4 | 10-K Item 1 / 決算説明会への導線 | 事業理解は人間が行う(原則1) | ❌ | **J-5**(チェックリストの外部リンク) |
| B5 | TAM・モート・顧客集中・ユニットエコノミクス | 10倍の上限を決めるのはここ | △ | ノートの自由記述欄のみ(意図どおり)。記入状況の可視化は **J-5** |

### 段階C. 数字の理解(実績はどうなっているか)

| # | 必要な情報 | なぜ必要か | 状態 | ギャップ |
|---|---|---|---|---|
| C1 | 売上の年次・四半期推移 | 成長の実在と加速/減速 | ❌ | **J-2**(payload にあるが未表示) |
| C2 | 粗利率の推移 | 価格支配力。第2因子の実績 | ❌ | **J-2** |
| C3 | 営業CF・FCF・バーンレート | 生存と資金サイクル | △ | ゲート内部でのみ使用。**J-2** |
| C4 | 現金・有利子負債・ネットデット | ダウンサイドの床 | △ | 因子 `leverage_effect` のみ。**J-2** |
| C5 | 発行済株式数の推移 | 希薄化の実績(分母) | △ | 因子 `dilution_drag` のみ。**J-2** |
| C6 | ランウェイ月数 | 増資の必然性 | △ | ゲートと保有モニタリングのみ。候補画面に無い。**J-2** |
| C7 | Piotroski F-score の内訳 | 成長の質(fade に効いている) | △ | 合成値のみ。9項目の内訳が出ない。**J-2** |
| C8 | SEC 原本との突合 | 二次データの検算 | ✅ | `validation/reconciliation.py` |

### 段階D. バリュエーション(いま払う価格は妥当か)

| # | 必要な情報 | なぜ必要か | 状態 | ギャップ |
|---|---|---|---|---|
| D1 | 現在の EV/粗利・EV/売上 | 成長の対価を既にいくら払っているか | △ | `current_ev_to_gross_profit` はあるが「高いのか安いのか」が無い。**J-3** |
| D2 | セクター内・ユニバース内の分位 | 相対的な割高割安 | ❌ | **J-3** |
| D3 | 自社の過去1年のバリュエーション推移 | 直近の織り込みの変化 | ❌ | **J-3**(`scores.factors` に日次で貯まっている) |
| D4 | 株価の52週レンジ内の位置 | 入る位置の把握 | ❌ | **J-3**(`price_snapshots` にある) |
| D5 | 終端マルチプルの前提 | モデルが何を仮定したか | ✅ | `target_ev_to_gross_profit`・`multiple_change` |

### 段階E. 見通しとリスクの定量化

| # | 必要な情報 | なぜ必要か | 状態 | ギャップ |
|---|---|---|---|---|
| E1 | 期待倍率と中央値倍率 | 中心的な見通し | ✅ | `expected_moic` / `median_moic` |
| E2 | P(目標達成) | 右裾に届く確率 | ✅ | `probability`・較正済み1年オンペース率 |
| E3 | 下振れ確率 | −50%以下・元本割れの頻度 | ✅ | `probability_below_half` / `_below_one` |
| E4 | **分位点(P10/P50/P90 の実現倍率)** | 「どのくらい外すか」の幅 | ❌ | **J-4**(`log_moic_mu`・`sigma` から即出せる) |
| E5 | 生存確率 | 7年後に存在しない確率 | ✅ | `survival_probability` |
| E6 | 因子ごとの寄与 | 何が効いて上位なのか | ✅ | `factor_breakdown` |
| E7 | クランプ・外挿限界の警告 | モデルの外挿が効いているか | ✅ | `warnings`(`growth_rate_clamped` 等) |
| E8 | データ鮮度 | 何日前の数字で判断しているか | ✅ | `data_age_days`・鮮度ガード |
| E9 | **モデル自体の信頼度** | 順位を信じてよいか | △ | `/validation` は充実。ただし**現在 KPI は FAIL**。**J-0** |

### 段階F. 即死要因と一次情報

| # | 必要な情報 | 状態 | ギャップ |
|---|---|---|---|
| F1 | 継続企業の前提・内部統制の重要な不備 | ✅ | `screening/red_flags.py` |
| F2 | 提出遅延(NT 10-K/10-Q)・上場基準通知 | ✅ | 同上 |
| F3 | SEC コメントレター | ✅ | 同上 |
| F4 | シェルフ・ATM・転換条項(将来の希薄化) | △ | 自動分は実装済、条項は人間の入力欄(意図どおり) |
| F5 | 訴訟・ショートレポート | ❌ | 外部検索リンクのみの予定が未実装。**J-5** |

### 段階G. 執行(買えるか・いくら・いつ)

| # | 必要な情報 | 状態 | ギャップ |
|---|---|---|---|
| G1 | 証券会社の取扱可否 | ✅ | `screening/tradability.py` |
| G2 | ADV と参加率上限 | ✅ | `screening/liquidity.py` |
| G3 | 往復コスト(スプレッド+インパクト) | ✅ | `screening/trading_cost.py` |
| G4 | ポジション上限(規律・ADV・二値イベント) | ✅ | `config/portfolio.yaml` |
| G5 | **次回決算日などのカタリスト** | ❌ | **J-6** |
| G6 | **需給(インサイダー・空売り残・浮動株)** | ❌ | パーサのみ存在、未配線。**J-7** |
| G7 | 円換算での金額感 | ❌ | **J-10**(表示のみ。税務・為替計算はスコープ外のまま) |

### 段階H. 保有・監視・売却・記録

| # | 必要な情報 | 状態 | ギャップ |
|---|---|---|---|
| H1 | 保有一覧・含み損益・比率 | ✅ | `/positions` |
| H2 | セクター上限・銘柄上限の抵触 | ✅ | 同上 |
| H3 | 四半期モニタリング指標 | ✅ | `screening/monitoring_metrics.py` |
| H4 | 未解消アラート | ✅ | `/alerts` |
| H5 | 投資ノートの記入漏れ検出 | ✅ | `research/notes.py` |
| H6 | **売却規律(テーゼ崩壊条件・利食い計画)** | ❌ | ノートに欄が無い。**J-8** |
| H7 | **達成倍率と計画の対比** | ❌ | **J-8** |
| H8 | **保有群のポートフォリオ見通し(相関込み)** | ❌ | ランキング画面にはあるが保有画面に無い。**J-9** |
| H9 | 売買記録の不可逆性 | ✅ | git 管理の `config/positions.yaml`(原則2) |
| H10 | 免責と限界への導線 | ❌ | 30.9.3 未実装。**J-5** |

---

## 3. ギャップ一覧(優先順)

| ID | 内容 | 種別 | 追加収集 | マイグレーション | 規模 |
|---|---|---|---|---|---|
| **J-0** | 生存バイアス修正の**運用実行**とKPI再判定 | 運用 | EDGAR(実装済) | 不要 | 半日+実行時間 |
| **J-5** | デューデリ・チェックリスト(11工程)と免責表示 | フロントのみ | 不要 | 不要 | 1日 |
| **J-1** | 会社概要の表示(事業内容・IR・上場情報) | 表示 | 不要 | 不要 | 半日 |
| **J-2** | 財務推移の表示(売上・粗利率・CF・現金・株式数・ランウェイ・F-score内訳) | 表示 | 不要 | 不要 | 1.5日 |
| **J-3** | バリュエーションの現在地(断面分位・自社推移・52週位置) | 算出+表示 | 不要 | 不要(JSONB) | 1日 |
| **J-4** | 実現倍率の分位点(生存確率込みの混合分布) | 算出+表示 | 不要 | 不要 | 半日 |
| **J-6** | カタリスト・カレンダー(次回決算日・検証日) | 収集+表示 | yfinance | 要(1テーブル) | 1.5日 |
| **J-8** | 売却規律の器と達成度の対比 | 器+表示 | 不要 | 不要 | 1日 |
| **J-9** | 保有群のポートフォリオ見通し | 表示 | 不要 | 不要 | 半日 |
| **J-7** | 需給(Form 4・空売り残・浮動株)の配線 | 収集+表示 | SEC / FINRA | 要(2テーブル) | 2日 |
| **J-10** | 円換算表示 | 収集+表示 | FRED or yfinance | 不要 | 半日 |

**実装順序**:J-0 → J-5 → J-1 → J-2 → J-3 → J-4 → J-6 → J-8 → J-9 → J-7 → J-10。
J-6・J-7 はマイグレーションを持つので、30.11 のとおり**並行着手しない**。

---

## 4. 実装計画

各項目は 30.11 の手順(設定 → 純粋関数 → マイグレーション → バッチ → API → フロント → README → 受け入れ基準)に従う。

### J-0. モデル検証の回復(最優先・実装ではなく運用)

**なぜ最初か。** ランキングが投資判断の入力として使えるかどうかがここで決まる。
新しい表示を足しても、順位が生存バイアスで歪んだままなら「よく見える誤りが増える」だけになる。

**手順**

1. `uv run python -m autoscreener.cli recover-quarantine`
   隔離された 5,312 銘柄を復帰させる。完了後 `GET /api/v1/universe/status` の
   `collection_status_counts` を確認する。
2. `.env` に `EDGAR_USER_AGENT`(連絡先メールを含む文字列)が入っていることを確認し、
   `uv run python -m autoscreener.cli collect-delistings` を実行する。
   SEC の form.idx を遡って Form 25-NSE / 15-12B から上場廃止イベントを登録する。
3. `uv run python -m autoscreener.cli register-benchmarks`(IWM/IWC/IJR/SPY)と
   `backfill-history` を実行し、超過CAGR の分母を用意する。
4. `uv run python -m autoscreener.cli run-backtest` を実行し、次を記録する。
   - `delisted_settlement_rate` が 0.00% から動いたか(動かなければ 2 が効いていない)
   - `kpi_verdicts` の各項目(PASS / FAIL / INSUFFICIENT_DATA)
   - `baselines` に対する v4 の優位(D-8。ここが本丸)
5. 結果を `README.md` 冒頭の警告ブロックに反映する。

**受け入れ基準**

- [ ] `delisted_settlement_rate > 0`
- [ ] `effective_dates`(Kish)と各KPIの95%信頼区間が `/validation` に表示される
- [ ] v4 が `momentum_12m` / `revenue_growth` ベースラインを**信頼区間つきで**上回るか否かが明文で書かれている
- [ ] 上回らない場合、README とランキング画面に「順位はベースラインと区別できていない」と明記する

> **重要**:4 の結果が「区別できない」だった場合、**それ自体が投資判断に必要な情報**である。
> 隠さずに出す。以降の J-1〜J-10 は、その状態でも価値がある(いずれもモデルの順位ではなく
> 一次的な事実の提示であるため)。

---

### J-5. デューデリ・チェックリストと免責表示(30.9.2 / 30.9.3 の未実装分)

**なぜ2番目か。** バックエンド変更ゼロで、**判断の手順そのもの**を画面に載せられるため。
既存の API(`/candidates/{ticker}`・`/research/{ticker}`)だけで11工程の状態が決まる。

**実装**

- 新規 `frontend/src/components/DueDiligenceChecklist.tsx`
  - 入力:`CandidateDetail` と `ResearchNoteResponse`
  - 各工程を3状態で描画(`auto` 判定済 / `recorded` 記録済 / `todo` 未着手)
  - 判定の対応:

    | 工程 | 状態の決め方 |
    |---|---|
    | 01 取扱可否 | `tradability` と `tradable_brokers` |
    | 02 流動性 | `adv_usd` / `max_position_usd` / `position_binding_constraint` |
    | 03 即死要因 | `red_flags` の severity(blocking があれば ⚠) |
    | 04 原本照合 | `sec_reconciliation` の status 集計 |
    | 05 希薄化 | `dilution_outlook` の各欄と、ノートの `dilution` |
    | 06 事業の理解 | ノート本文の有無(J-1 の会社概要セクションへのアンカーも出す) |
    | 07 経営陣の検証 | ノート `assumptions` / 自由記述 |
    | 08 反証 | ノート `premortem` が3件以上か |
    | 09 サイジングと記録 | `note_missing_fields` |
    | 10 執行 | `estimated_round_trip_cost_bps` |
    | 11 検証日 | ノート `verification_date`(J-6 で自動化) |
- 外部リンク(30.9.2 のとおり)
  - EDGAR:`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K`
  - 会社IR:`payload.info.website`(J-1 で API に載せる)
  - ショートレポート検索、`https://securities.stanford.edu`(証券集団訴訟)
  - **CIK が無い銘柄ではリンクを出さず「CIK 未解決(`refresh-cik-map` を実行)」と表示する**
- 免責:`Layout.tsx` のフッターに常時表示。文面は 30.9.3 のとおりで、`/validation` へのリンクを必須にする。
- `glossary.ts` に「デューデリジェンス」「プレモーテム」「シェルフ登録」「ATM」を追加(未登録分のみ)。

**テスト**:判定ロジックは純関数 `deriveChecklist()` に切り出し、`warnings.ts` と同じ層に置く
(フロントに vitest 基盤が無いため、テスト基盤の新設はこの計画の対象外。型とビルドで担保する)。

**受け入れ基準**

- [ ] 11工程がすべて表示され、3状態が視覚的に区別できる
- [ ] CIK がある銘柄で外部リンクが正しく開く
- [ ] 免責がすべての画面のフッターに出て、`/validation` へ遷移できる
- [ ] `npm run build` と `npm run lint` が通る

---

### J-1. 会社概要の表示

**データ源**:`raw_snapshots.payload.info`(既に 16,108 行に `longBusinessSummary` あり)。
追加収集は不要。`tickers` の `listed_date` / `cik` / `sector` / `industry` も併せて出す。

**実装**

1. `api/schemas.py` に追加:
   ```python
   class CompanyProfile(BaseModel):
       business_summary: str | None = None   # info.longBusinessSummary(原文のまま)
       website: str | None = None
       industry: str | None = None
       country: str | None = None
       full_time_employees: int | None = None
       exchange: str | None = None
       listed_date: datetime.date | None = None
       cik: str | None = None
       profile_as_of: datetime.date | None = None   # raw_snapshots.snapshot_date
   ```
   `CandidateDetail` に `profile: CompanyProfile | None = None` を足す。
2. `api/routes.py` の `get_candidate_detail`(851行〜)で、既存の
   `_latest_raw_snapshots_by_ticker` を単一銘柄に使って `payload["info"]` から詰める。
   `info` 欠損時は `profile=None` を返し、**例外にしない**(185 行が該当)。
3. `frontend/src/pages/TickerDetailPage.tsx` の**先頭**(スコアより上)に
   「この会社は何をしているか」セクションを置く。長文なので3行で折り畳み、
   「全文」で展開。`website` と EDGAR へのリンクを併記。

**設計上の判断**

- **原文をそのまま出す。要約も翻訳も生成しない**(原則1。生成要約は「読んだつもり」を作る)。
- `info` は日次で上書きされる二次情報なので**表示専用**とし、スコアリング・ゲートからは参照しない。
- `profile_as_of` を必ず出す。事業内容の記述が古い可能性を隠さない。

**テスト**(`tests/unit/test_api_routes.py`)

- [ ] `info` に `longBusinessSummary` を含む payload → `profile.business_summary` が返る
- [ ] `info` が空 → `profile is None` で 200 が返る
- [ ] `listed_date` / `cik` が `tickers` から入る

**受け入れ基準**

- [ ] ランキング上位10銘柄すべてで事業概要が表示される
- [ ] 事業概要が無い銘柄でも詳細画面がエラーにならない

---

### J-2. 財務推移の表示

**データ源**:`raw_snapshots.payload` の `income_stmt` / `balance_sheet` / `cash_flow` /
`quarterly_income_stmt` / `quarterly_cash_flow`。追加収集不要。

**実装**

1. 新規純関数モジュール `src/autoscreener/screening/financial_history.py`
   - `build_financial_history(payload: dict) -> FinancialHistory`
   - 既存の `scoring/point_in_time.py` の
     `build_point_in_time_statements` / `_series` / `_latest` / `financial_to_trading_rate` を
     **再実装せずに再利用する**(行名の表記ゆれ対応が既にそこにあるため)。
     表示ではポイントインタイム・フィルタは不要なので、可視期間を全期に開いて呼ぶ。
   - 返す系列(年次最大4期・四半期最大5期):
     売上高、粗利、粗利率、営業利益、純利益、営業CF、設備投資、FCF、
     現金同等物、有利子負債合計、ネットデット、発行済株式数
   - 派生値:YoY 成長率、四半期の平均バーンレート、**ランウェイ月数**、
     株式数の年率増加率、Piotroski F-score の**9項目の内訳**
     (`scoring/financial_metrics.piotroski_f_score` の `PiotroskiResult` に内訳が無ければ露出させる)
   - **通貨**:`financial_to_trading_rate` で取引通貨に統一してから返す
     (2026-08-26 の欠陥「通貨混在による EV 誤り」の再発防止)
2. API:`GET /api/v1/candidates/{ticker}/financials` を新設(詳細本体を重くしないため別エンドポイント)。
   スキーマ `FinancialHistoryResponse { currency, annual: [...], quarterly: [...], derived: {...}, as_of }`。
3. フロント:詳細画面に「実績の推移」セクション。
   - 売上と粗利率の2軸チャート(`recharts` は既に依存にある)
   - 現金・ネットデット・株式数のテーブル
   - ランウェイ月数はゲート閾値(`cash_runway_floor`)を並記し「あと何ヶ月で増資が要るか」を示す
   - F-score は9項目のチェックリスト表示(fade に効いている根拠を見せる)

**テスト**(`tests/unit/test_financial_history.py` 新規)

- [ ] 年次4期・四半期5期の payload から正しい系列が出る
- [ ] 決算通貨 ≠ 取引通貨のとき換算される
- [ ] 行名が欠けている(`Gross Profit` が無い)payload で例外にならず `None` が入る
- [ ] FCF が正の企業ではランウェイが `None`(無限)になる
- [ ] F-score 9項目の合計が既存 `piotroski_f_score` の値と一致する

**受け入れ基準**

- [ ] 売上・粗利率・現金・株式数の推移が上位銘柄で表示される
- [ ] 既存の `apply-gates` / `run-scoring` の出力が1銘柄も変わらない(表示層の追加であること)

---

### J-3. バリュエーションの現在地

**目的**:`current_ev_to_gross_profit` が 12.4 だとして、それが**高いのか安いのか**を示す。

**実装**

1. `scoring/engine.py` の断面計算(既に `cross_section` を作っている箇所)に分位を追加し、
   `factors`(JSONB。**マイグレーション不要**)へ格納する:
   - `ev_to_gross_profit_percentile_universe`
   - `ev_to_gross_profit_percentile_sector`
   - `revenue_growth_percentile_sector`
   - `gross_margin_percentile_sector`
   - **セクター内標本が 20 未満なら計算しない**(14.7 のセクター相対の頑健性)。
     `None` を入れ、UI は「セクター標本が少ないため非表示」と出す。
2. 自社の推移:`scores` は日次で `factors` を持っているので、
   `GET /api/v1/candidates/{ticker}` の `score_history` に `ev_to_gross_profit` を併記する。
3. 52週レンジ内の位置:`price_snapshots` から
   `week52_high` / `week52_low` / `position_in_range`(0〜1)を計算して詳細に載せる。
   純関数は `screening/price_range.py` に置く。
4. フロント:詳細画面の「バリュエーション」セクション。
   分位はバー、自社推移は既存 `ScoreHistoryChart` に系列を追加。

**設計上の注意**

- これは**モデルの入力ではない**。κ による成長の対価の差し引き(28.2)はモデル内部で既に
  行われており、ここで足すのは**人間が読むための断面情報**である。順位計算には一切影響させない。
- 分位は「同じ日の断面」でのみ切る。日付をまたいでプールしない(D-4 の再発防止)。

**テスト**

- [ ] 分位が 0〜1 に収まり、最小値が 0、最大値が 1 になる
- [ ] セクター標本 19 件では `None`、20 件では値が入る
- [ ] 52週レンジ:高値=安値(値動きなし)で 0 除算しない

**受け入れ基準**

- [ ] 同一日の `run-scoring` を分位追加の前後で比較し、`probability` が全銘柄で不変

---

### J-4. 実現倍率の分位点(生存確率込み)

**目的**:「期待倍率 2.4 倍」だけでは幅が分からない。P10/P50/P90 を出す。

**実装**

1. `scoring/moic.py` に純関数を追加:
   ```python
   def moic_quantiles(
       log_mu: float, log_sigma: float, survival_probability: float,
       quantiles: Sequence[float] = (0.10, 0.25, 0.50, 0.75, 0.90),
   ) -> dict[float, float]:
   ```
   **混合分布として扱う**:確率 `1 - S` で結果は ≈0(倒産・上場廃止)、確率 `S` で対数正規。
   したがって累積確率 `q <= 1 - S` の分位点は `0.0` を返し、それ以外は
   `exp(mu + sigma * Phi^-1((q - (1-S)) / S))` を返す。
   **この扱いを省くと、生存確率 0.6 の銘柄の P10 を対数正規だけで出してしまい、
   ダウンサイドを構造的に過小評価する。**
2. API:`CandidateDetail.moic_quantiles: dict[str, float] | None`。
   一覧にも `moic_p10` / `moic_p90` を任意フィールドで追加(**ソート対象にはしない**)。
3. フロント:詳細画面に横バー1本(P10 — P50 — P90、目標倍率のラインを重ねる)。
   一覧では「期待 2.4倍(0.0 — 8.7)」の形で併記。

**表示上の注意(必ず書く)**

- 分位点は**生の対数正規から出す**。較正(28.8)は閾値超過確率にしか掛かっていない単調写像なので、
  分位点には適用できない。画面に「この幅はモデルの仮定によるもので、実測で較正されていない」と明記する。
- `sigma_shrinkage: 0.85` により σ の銘柄差は 15% しか残っていない。
  つまり**幅はほぼ全銘柄で似た形になる**。これも注記する(隠すと「銘柄ごとにリスクを測れている」
  という誤解を生む)。

**テスト**

- [ ] `survival_probability = 1.0` で対数正規の分位点と一致
- [ ] `survival_probability = 0.85` のとき P10 = 0.0
- [ ] 分位点が単調増加
- [ ] P50 が `median_moic` と整合(生存確率調整後)

---

### J-6. カタリスト・カレンダー

**目的**:「いつ建てるか」「いつテーゼが試されるか」。現在 `verification_date` は手入力(30.10 項3)。

**ポイントインタイム安全性の設計(最重要)**

`earnings_dates` の収集は 27.16 で**意図的に止めた**。理由は「現在時点のスナップショットしか
取れず過去に遡れない → 使うとモデルが検証不能になる」。したがって復活させる場合は、
**スコアリングとバックテストから物理的に隔離する**。

- 保存先を `scores` / `raw_snapshots` から分離した専用テーブルにする
- `collected_on` を必ず持たせ、「いつ知った予定か」を残す
- `scoring/` と `backtest/` から当該モジュールを **import しないことをテストで固定する**

**実装**

1. マイグレーション:`event_calendar`
   ```
   id, ticker_id(FK), event_type('earnings'|'verification'|'manual'),
   event_date, is_estimated(bool), source(str), collected_on(date), created_at
   UNIQUE(ticker_id, event_type, event_date)
   ```
2. 収集:`collectors/calendar_source.py`(yfinance `Ticker.calendar` の次回決算日のみ採用。
   過去日は捨てる)。対象は 30.3.4 の**追跡対象銘柄**(保有 + 上位N + ノートあり)に限定し、
   全銘柄には広げない(レート制限 8.3・14.9)。
3. バッチ/CLI:`batch/collect_events.py`、`cli collect-events`。
   `run-daily-pipeline` の**週次(月曜)工程**に追加。失敗してもパイプラインは止めない。
4. ノートの `verification_date` は `event_type='verification'` として同じテーブルに読み込む
   (書き込みはしない。ノートが正、DB は索引)。
5. API:`GET /api/v1/calendar?days=30`(近い順)、`CandidateDetail.next_event`。
6. フロント:新規 `/calendar` ページ + ランキング行に「決算まで N 日」バッジ。
   **「決算前に建てるな」とは書かない**——それは判断であり、アプリは日数だけ出す。

**テスト**

- [ ] `Ticker.calendar` が空 / 過去日のみのケースで行を作らない
- [ ] 同じ日付を再収集しても重複行にならない(UNIQUE)
- [ ] **`scoring` と `backtest` のソースに `event_calendar` の文字列が現れない**ことを
      assert するテストを置く(ポイントインタイム汚染の再発防止)

---

### J-8. 売却規律の器と達成度の対比

**目的**:買う前に降り方を決める(元文書 第11節)。現在ノートに欄が無い。

**実装**

1. `research/TEMPLATE.md` に追加:
   ```yaml
   exit_plan:
     thesis_break:            # テーゼが壊れたと判断する条件(3件以上)
       - condition: 粗利率が3四半期連続で低下
         indicator: gross_margin_decline
     trim_rule:               # 利食い計画。機械実行はしない
       - at_moic: 3.0
         action: 1/3 を売却して原資を回収
       - at_moic: 6.0
         action: さらに 1/3
     max_hold_review_months: 24   # 何もなくても再検討する期限
   ```
2. `research/notes.py` の必須項目に `exit_plan.thesis_break`(3件以上)と `exit_plan.trim_rule` を追加。
   既存ノートは `missing_fields` に出るだけで、API は落ちない。
3. `api/routes.py` の `list_positions` に:
   - `achieved_moic` = 現在値 ÷ 取得単価
   - `next_trim`:`trim_rule` のうち未到達で最小の `at_moic` と、そこまでの倍率
   - `thesis_break_hits`:`monitoring_metrics` の点灯コードと `thesis_break[].indicator` の突合結果
4. `batch/run_monitoring.py`:`achieved_moic` が `trim_rule` の閾値を超えたら
   `severity='info'` のアラート `trim_threshold_reached` を1回だけ発火(重複抑止は既存の
   alerts の一意制約に合わせる)。
5. フロント:`PositionsPage` に「達成倍率 / 次の計画 / テーゼ点灯」の3列を追加。

**必ず画面に書く一文**(`config/monitoring.yaml` 冒頭と同じ立場):

> 閾値は売却条件ではない。点灯は「価格に関係なく判断をやり直す」合図であり、
> 機械的な売りシグナルとして使ってはならない。

**テスト**

- [ ] `exit_plan` 欠落ノートで `missing_fields` に出る/API は 200
- [ ] `achieved_moic` が 3.2 のとき `next_trim.at_moic == 6.0`
- [ ] 同じ閾値で2回アラートが出ない

---

### J-9. 保有群のポートフォリオ見通し

**実装**:`api/routes.py` の `_portfolio_outlook`(818行〜)を `list_positions` からも呼ぶ。
保有銘柄の最新 `probability` を渡し、相関込みの「少なくとも1つ当たる確率」を出す。
併せて `cash_ratio`(`portfolio_value_usd` − 取得原価合計)と、
**保有と現在のランキング上位の重複**(同じテーゼに二重に賭けていないか)を返す。

**テスト**:保有0件で `None` を返す / 1件で `probability_at_least_one == probability`。

---

### J-7. 需給の配線(Form 4・空売り残・浮動株)

**前提**:パーサは実装済み(`collectors/form4_source.py`・`collectors/short_interest_source.py`)。
足りないのは保存先・バッチ・API・UI。

**原則3 の徹底**:これらは**ゲートにもスコアにも入れない**。表示とアラートのみ。
FINRA の空売り残は月2回・数営業日遅れであり、**遅延日数を必ず画面に出す**
(30.1.3 が「対象外」とした理由がこれである以上、出すなら鮮度の明示は必須条件)。

**実装**

1. マイグレーション:
   - `insider_transactions`(ticker_id, accession_number, filed_date, transaction_date,
     insider_name, role, transaction_code, shares, price_usd, value_usd, is_derivative)
     UNIQUE(accession_number, insider_name, transaction_date, transaction_code, shares)
   - `short_interest`(ticker_id, settlement_date, short_interest_shares, avg_daily_volume,
     days_to_cover, published_date) UNIQUE(ticker_id, settlement_date)
2. バッチ:`batch/collect_supply.py`、CLI `collect-insider` / `collect-short-interest`。
   追跡対象銘柄のみ。週次(月曜)。
3. 浮動株:`xbrl_facts` の `public_float`(B-3 で CONCEPT_TAGS に追加済み)を API に露出し、
   `float_ratio = public_float ÷ market_cap` を出す。
4. API:`CandidateDetail.supply`
   `{ insider_net_shares_180d, insider_buyer_count_180d, insider_as_of,
      short_interest_shares, days_to_cover, short_as_of, short_lag_days,
      public_float_usd, float_ratio }`
5. フロント:詳細画面「需給」セクション。バッジは出すが**警告色にしない**
   (インサイダー売却は権利行使・納税・分散のいずれでも起きる。色で断定しない)。

**受け入れ基準**

- [ ] `evaluate_gates` と `run-scoring` の出力が1銘柄も変わらない
- [ ] 空売り残の `short_lag_days` が画面に出る
- [ ] データが無い銘柄で「未取得」と表示され、0 と区別できる

---

### J-10. 円換算表示

**実装**:`macro_series` に USDJPY を追加(FRED `DEXJPUS`。`FRED_API_KEY` 未設定時は
yfinance の `JPY=X` にフォールバック)。フロントに通貨トグル(`localStorage` 保存)を置き、
時価総額・ADV・ポジション上限・保有評価額を円で併記する。

**やらないこと(30.1.3 のまま)**:税務計算、取得為替レートでの損益計算、確定申告用の出力。
表示用の換算と税務計算は別物であり、後者は制度追随の負債になる。

---

## 5. この計画で実装しないもの(と理由)

- **決算説明会トランスクリプトの取得・要約**:安定した無料取得先が無く、要約生成は原則1に反する。
- **TAM・モート・経営者評価の自動化**:同上。ノートの器と外部リンク(J-5)までとする。
- **13F(機関投資家保有)**:四半期・45日遅れで、マイクロキャップでは保有主体が少なく情報量が薄い。
- **ニュース・センチメント**:出典が分散し、規約が各社異なる。
- **アナリスト目標株価・コンセンサス**:小型株ではカバレッジが 0〜2 社で、そもそも存在しない銘柄が大半。
- **税務・為替の損益計算**:制度改正に追随できないものを表示するのは、表示しないより悪い。
- **上場廃止銘柄の履歴データの購入**:調達の問題であり実装の問題ではない。R-1 の本丸は J-0 の
  EDGAR 経路で部分的にしか埋まらない、という事実は README に残し続ける。

---

## 6. 作業チェックリスト

- [ ] J-0:`recover-quarantine` → `collect-delistings` → `register-benchmarks` → `run-backtest` → README 更新
- [ ] J-5:チェックリスト+免責(フロントのみ)
- [ ] J-1:会社概要(API + フロント)
- [ ] J-2:財務推移(純関数 + API + フロント)
- [ ] J-3:バリュエーションの現在地(engine の factors + API + フロント)
- [ ] J-4:分位点(moic 純関数 + API + フロント)
- [ ] J-6:カタリスト(マイグレーション → 収集 → API → フロント)
- [ ] J-8:売却規律(テンプレ → notes.py → positions API → monitoring → フロント)
- [ ] J-9:保有群のポートフォリオ見通し
- [ ] J-7:需給(マイグレーション → 収集 → API → フロント)
- [ ] J-10:円換算

各項目の完了条件は共通で次の3つ。

1. `uv run pytest` が通る
2. `cd frontend && npm run build && npm run lint` が通る
3. **同一日の `run-scoring` を変更前後で比較し、`probability` が全銘柄で不変**
   (J-0 を除く全項目は表示・記録層の追加であり、順位を動かしてはならない)
