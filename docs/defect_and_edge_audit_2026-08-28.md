# 欠陥監査と情報優位の設計 — 2026-08-28

対象: `src/autoscreener/` 全体、`config/*.yaml`、本番DB(`autoscreener`)の実データ。
既存の監査文書(`defect_audit_2026-08-27.md` 等)は**参照せず**、コードと実データのみから独立に導出した。

実施した検証:
- `pytest tests/unit -q` → **451 passed**(実装の内的整合性は高い)
- 本番DBへの直接クエリ(付録に生の数字を記載)
- `backtest_runs` 最新実行 (id=28, 2026-08-26, horizon 365d, 3,107観測) のメトリクス全読み

---

## 0. 結論(3行)

1. **モデルの品質を測っている土台が壊れている。** 擬似バックテストの母集団は
   100%が現在も上場している銘柄であり(実測で確認)、v4のパラメータは全部
   この標本の上でKPIを比較して選ばれている。**モデルを直す前に標本を直すこと。**
2. **絶対性能を一度も測っていない。** ベンチマークもポートフォリオ・シミュレーションも
   取引コストも存在しない。「上位デシルのリフト1.50」は、そのユニバース自体が
   指数に負けていても成立する数字である。
3. **プロに勝てる余地は情報量ではなく構造(制約の非対称性)にしかないが、
   その構造を測る指標がアプリに1つも無い。** 追加すべきデータは全部無料で、
   しかも既に持っている EDGAR クライアントの延長線上にある。

---

## 1. 欠陥一覧(重大度順)

各項目は **症状 / 証拠 / 機序 / 影響 / 修正案** の順。修正案は実装者がそのまま
着手できる粒度で書いた。**すべての修正は「KPIが改善するか」ではなく
「KPIが信用できるようになるか」で評価すること**(D-1, D-2 を直すまで KPI 比較に
意味は無い)。

---

### D-1【致命的】擬似バックテストの母集団が100%生存者である

**症状**: 3年分のバックテストに、上場廃止・買収・破綻した銘柄が1件も入っていない。

**証拠**(本番DB、2026-08-28 実測):

```
SELECT count(*) FROM tickers;                               -> 5312
SELECT count(*) FROM tickers WHERE delisted_at IS NOT NULL; -> 0
-- 価格履歴を持つ全銘柄の最終取引日
SELECT bucket, count(*) FROM (
  SELECT ticker_id, CASE WHEN max(trade_date) >= DATE '2026-08-20'
    THEN 'active' ELSE 'stopped' END AS bucket
  FROM price_snapshots GROUP BY ticker_id) t GROUP BY 1;
  -> [('active', 5286)]        -- 「途中で価格が途切れた銘柄」が 0 件

-- backtest_runs id=28 のメトリクス
delisted_settlement_rate = 0.0
```

`backtest/metrics.py` の `BacktestMetrics.delisted_settlement_rate` には
コメントで「**0%なら生存バイアスを疑う**」と書いてある。実測値はちょうど 0.0 である。
アプリ自身の検知装置が、設計者の意図どおりに鳴っている。

**機序**: `collectors/universe_source.py::fetch_universe_candidates` は
NASDAQ Trader の **今日の**シンボルディレクトリ(`nasdaqlisted.txt` /
`otherlisted.txt`)を取得する。過去のディレクトリは配布されていない。
`batch/backfill_history.py` はそこで得た銘柄についてのみ3年分の価格を取る。
したがって 2023-08〜2026-08 のあいだに消えた企業は `tickers` に**一度も存在しない**。

`backtest/runner.py::_realized_return` の上場廃止決済(27.11)は、
「`tickers` に居るが価格が途切れた銘柄」しか救えない。上記のとおりそれは 0 件である。
つまり 27.11 の修正は**論理的には正しいが、実データでは一度も発火していない**。

**影響**: 米国マイクロキャップの年間上場廃止率は概ね 4〜7%。3年で母集団の
15〜20% が失われ、その内訳は
(a) 破綻・上場基準抵触による −80〜−100%、
(b) 買収による +20〜+100% の一発、
という**両裾**である。裾が両方消えた標本の上で:

- `lift_ratio = 1.50`、`decile_monotonicity = 0.806`、`rank_ic = 0.152` を測った
- `calibration_map`(28.8)を学習した = **表示している確率そのものが生存者標本の頻度**
- `survival.base_annual_hazard = 0.06` / `health_sensitivity = 1.2` を「事前値」として
  据え置いた。**上場廃止を補正する項を、上場廃止が1件も無い標本で検証している**という循環
- `config/scoring.yaml` に記録された全パラメータ選定
  (`nowcast_weight`, `sigma_shrinkage`, `margin.max_relative_change`,
  `max_initial_rate_single_observation`, `growth_elasticity`, ユニバース上限 11.7B)

**修正案 D-1**:

段階1(必須・独立して価値がある):**上場廃止銘柄ユニバースの構築**

新規モジュール `src/autoscreener/collectors/delisting_source.py`:

1. SEC EDGAR の四半期インデックスを全期間走査する。
   `https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx`
   は `Form Type | Company Name | CIK | Date Filed | File Name` のパイプ区切りで、
   認証不要・`edgar_client.RateLimiter` をそのまま使える。
2. 上場廃止を示すフォームを抽出する:
   `25`, `25-NSE`(取引所による上場廃止届)、`15-12B`, `15-12G`, `15F-12B`
   (登録抹消)。対象期間は価格履歴の開始(現状 2023-08)より1年前から今日まで。
3. CIK → ティッカーの解決。`edgar_client` が既に取っている
   `https://www.sec.gov/files/company_tickers.json` に加え、
   `https://data.sec.gov/submissions/CIK{cik}.json` の `tickers` と
   `formerNames` を使う(廃止済み企業も submissions は残る)。
4. 解決できたシンボルを `tickers` に `delisted_at = <Form 25/15 の提出日>` 付きで登録。
   **`is_quarantined = True` にしないこと**(日次収集の対象からは
   `delisted_at IS NOT NULL` で別途外す。既存の `apply_gates` はもう見ている)。
5. `backfill_history` を廃止銘柄にも走らせる。yfinance は廃止銘柄でも
   `history(period="max")` が過去分を返すことが多い。**返らなかった銘柄は
   捨てず** `tickers.price_unavailable = True`(新規カラム)で記録する。

段階2:**バックテストの母集団に組み込む**

- `runner.py::_load_payloads` は `raw_snapshots` に依存する。廃止銘柄には
  raw_snapshot が無いので、`build_moic_inputs` の入力を **XBRL companyfacts から作る**
  経路が要る(→ I-1 と一体で実装するのが正しい。単独では不可能ではないが二度手間になる)。
- `_realized_return` は現状のままで機能する(`final_close` の経路が初めて発火する)。
- **価格が取れなかった廃止銘柄**は `-1.0` で決済せず、`settlement="delisted_unpriced"`
  として別集計し、KPI を「含めた場合/除いた場合」の両方で出す。
  片方に決め打ちすると、その決め打ちが次の隠れバイアスになる。

**受け入れ基準**:
- `delisted_settlement_rate` が 3年ホライズンで **0.03〜0.15** のオーダーになること。
  0.0 のままなら実装が効いていない。
- 段階2の投入前後で、全KPI(lift / monotonicity / rank_ic / loss_rate / calibration_error)
  の変化を1つの表にして本文書の続編に記録すること。
  **KPIが悪化するのが正常な結果である。** 悪化しなかったら実装を疑うこと。
- `survival.base_annual_hazard` を、実測の年間廃止率(health デシル別)で置き換える。

---

### D-2【致命的】有効標本が実質3以下なのに、8評価日を独立として扱ってパラメータを選んでいる

**症状**: `config/scoring.yaml` の主要パラメータは、KPIの小数第2〜3位の差で選ばれている。
その差はノイズの幅より小さい。

**証拠**(backtest_runs id=28、`per_date`):

| 評価日 | 観測数 | lift_ratio | rank_ic |
|---|---:|---:|---:|
| 2023-11-21 | **20** | 1.67 | **0.435** |
| 2024-02-20 | **54** | 1.66 | 0.111 |
| 2024-05-21 | 496 | 1.03 | 0.034 |
| 2024-08-20 | 629 | 1.49 | 0.102 |
| 2024-11-19 | 501 | 1.49 | 0.126 |
| 2025-02-18 | 533 | 1.58 | 0.183 |
| 2025-05-20 | 432 | 1.63 | 0.159 |
| 2025-08-19 | 441 | 1.63 | 0.067 |

- 評価日は **8点**、間隔 91日、ホライズン 365日 → 保有期間が4重に重なる。
  **独立な観測期間は実質3つ以下。**
- 上位2行は n=20 と n=54。`per_date_stats` は `len(bucket) < 10` でしか切らないため、
  この2日が **n=629 の日と同じ重み** で `rank_ic` と最悪日リフトの平均に入る。
  この2日を除くと `rank_ic` は **0.152 → 0.112**、日次リフト平均は 1.55 → 1.475。
- `rank_ic_t_stat = 3.48`。`metrics.py::_t_stat` の docstring は自ら
  「評価日を独立とみなすので**上限値**」「有意性の主張として読んではいけない」
  と書いている。ところが `config/scoring.yaml` の `margin.max_relative_change`
  の採用根拠として「**t値 3.30→3.45**」がそのまま引かれている。禁止事項を
  自分で破っている。
- 重なりを補正すると t 値は概ね `3.48 × sqrt(3/8) ≈ 2.1`。n=20/54 の日を落とせばさらに下がる。

**影響**: 「単調性 0.830 → 0.867 に改善したので 2.0 を採用」
「1.3 では 0.830 → 0.758 に悪化」といった判断は、**すべてノイズの中の比較**である。
`sigma_shrinkage = 0.85`、`nowcast_weight = 0.25`、`max_initial_rate_single_observation = 0.45`
も同じ手続きで選ばれている。個々の判断が間違っているとは言えないが、
**根拠として主張されている強さが実際には存在しない**。

**修正案 D-2**:

1. `metrics.py::compute_metrics` に **観測数による重み付け**を入れる。
   `rank_ic` / `lift_ratio_worst_date` / `tail_lifts` は、
   評価日ごとの値を `count` で加重平均するか、`MIN_DATE_OBSERVATIONS = 100`
   未満の日を除外する。**既に `runner.py` の資産相関推定だけがこれをやっている
   (`_MIN_DATES_FOR_CORRELATION = 100`)。同じ基準を全KPIに広げるだけ。**

2. **ブロック・ブートストラップによる信頼区間**を `metrics.py` に追加する。
   ```python
   def bootstrap_kpi_interval(observations, kpi_fn, n_resamples=1000, seed=0) -> tuple[float, float]
   ```
   評価日単位でリサンプル(**銘柄単位ではない** — 同一日の銘柄は共通因子で相関している)。
   `BacktestMetrics` に `rank_ic_ci`, `lift_ratio_ci`, `decile_monotonicity_ci` を追加。

3. **非重複モードを主指標にする。** `run_backtest(interval_days >= horizon_days)`
   で重複ゼロの実行を必ず併走させ、`BacktestRun` に `overlapping: bool` を持たせる。
   現状の3年・1年ホライズンなら非重複は3点しか取れない。**それが正直な検出力である。**
   評価日を増やす唯一の道は履歴を伸ばすこと(→ I-1)。

4. **パラメータ変更の受け入れ手続きを CLI に組み込む。**
   新規サブコマンド `compare-configs <a.yaml> <b.yaml>`:
   両設定で同一観測に対しKPIを出し、**差がブートストラップCIを超えたときだけ
   "ADOPT" を、それ以外は "INDISTINGUISHABLE" を出力する**。
   `config/scoring.yaml` のコメントに書く実測値は、この出力を貼ること。

**受け入れ基準**: `config/scoring.yaml` の (c) 分類(擬似バックテストで選んだ値)の
全パラメータについて、`compare-configs` を再実行し、
**"INDISTINGUISHABLE" と出たものはコメントをそう書き換える**。
値を変える必要はない。**根拠の強さの記述を実態に合わせる**のが目的。

---

### D-3【重大】下落回避KPIが実質達成されていないのに、誰も落第と言っていない

**証拠**(backtest_runs id=28):
```
universe_loss_rate    = 0.10718   (-50%以下になった割合、全体)
top_decile_loss_rate  = 0.10443   (同、上位デシル)
```
相対で **2.6% の改善**。3,107観測・上位デシル316件では、この差は完全にノイズ。
14.2 の成功指標「破綻回避率(上位デシルの大幅下落率 < ユニバース平均)」は
**符号だけ合っていて実質は未達**である。

同様に `deciles` の中央値リターンは
`[0.171, 0.019, 0.022, 0.064, 0.029, -0.048, -0.103, -0.031, -0.126, -0.068]`
であり、**上位1デシル以外はほぼ順序が無い**(第4デシル 0.064 が第2・第3を上回り、
第10デシル −0.068 が第7・第9を上回る)。`decile_monotonicity = 0.806` は
10点のスピアマン相関で、この実態を過大に表現している。
実際に主張できるのは「**上位10%とそれ以外の差**」だけ。

**修正案 D-3**:
1. `config/scoring.yaml` に `kpi_acceptance:` ブロックを新設し、14.2 の目標値を
   機械可読にする(例 `min_lift_ratio: 1.5`, `max_top_decile_loss_ratio: 0.8`,
   `min_decile_monotonicity: 0.7`, `max_abs_calibration_error: 0.03`)。
2. `BacktestMetrics.as_dict()` に `kpi_verdicts: {name: PASS|FAIL|INSUFFICIENT_DATA}` を追加。
   `cli run-backtest` は FAIL があれば非ゼロ終了する。
3. API `/backtest/latest` と UI の検証画面に PASS/FAIL をそのまま出す。
   **単調性は「上位デシル vs 残り」の差(と、そのCI)を併記する。**
   10点スピアマンだけを見せない。

---

### D-4【重大】ベンチマークが存在しない — 絶対性能を一度も測っていない

**症状**: アプリのどこにも「このモデルに従ったら何%儲かったのか」を答える数字が無い。

**証拠**: `BacktestMetrics` のフィールドは全部**ユニバース内の相対量**
(lift = 上位デシル率 ÷ ユニバース率、単調性、順位IC)。
`macro_series` は FRED のみ(`collect_macro.py`)。
指数・ETF の価格は `price_snapshots` に一切入っていない
(`tickers` は NASDAQ Trader の候補由来で、`filter_candidates` が ETF を落としている)。

**影響**: 上位デシルのリフトが 1.50 でも、
ゲート通過ユニバース自体が指数に年率 −10% で負けていれば、
このアプリは「負け方が上手い銘柄群」を出しているだけになる。
`universe_median_return` はあるが比較対象が無い。
**プロに勝つ/負けるを議論する前に、市場に勝つ/負けるを測っていない。**

**修正案 D-4**:

1. **ベンチマーク銘柄の登録**。`tickers` に `is_benchmark: bool` を追加し
   (alembic migration)、`IWM`(Russell 2000)、`IWC`(マイクロキャップ)、
   `IJR`(S&P600)、`SPY` を登録。
   - `universe_source.filter_candidates` は通らないので `cli` に
     `register-benchmarks` を追加して手動投入。
   - `run_daily_collection` / `backfill_history` の対象に含める
     (`apply_gates` は `is_benchmark` を除外すること。ランキングに混ぜない)。

2. **ポートフォリオ・シミュレーションを `backtest/` に新設**
   (`backtest/portfolio_sim.py`)。今のバックテストは「観測ごとのリターンの集計」で
   あって、**ポートフォリオを一度も組んでいない**。
   ```python
   @dataclass(frozen=True)
   class PortfolioBacktest:
       equity_curve: list[tuple[date, float]]
       cagr: float
       max_drawdown: float
       volatility: float
       benchmark_cagr: dict[str, float]
       excess_cagr: dict[str, float]
       win_rate_vs_benchmark: float   # 評価日ごとに勝った割合
       turnover: float
       realized_cost_drag: float      # D-5 のコストモデル由来
   ```
   ルールは `config/portfolio.yaml` の既存値をそのまま使う
   (`max_positions=30`, `per_position_cap=0.04`, `sector_cap=0.25`)。
   評価日ごとに上位N銘柄を等金額で建て、次の評価日でリバランス。
   **上場廃止は D-1 の決済ロジックを通す。**

3. `BacktestMetrics` に `portfolio: PortfolioBacktest | None` を追加し、
   `backtest_runs.metrics` に保存、`/backtest/latest` で返す。

**受け入れ基準**: 「上位30銘柄・四半期リバランス・1年保有」の
**IWC 超過CAGRと最大ドローダウンが1つの数字で言えること。**
これが言えるまで、モデルの改良に着手しないこと。

---

### D-5【重大】取引コスト・約定可能性がモデルにもバックテストにも入っていない

**証拠**: `runner.py::_realized_return` は
`exit_price / entry - 1` をそのまま返す。手数料もスプレッドもマーケットインパクトも無い。
建玉は「翌営業日の**始値**」= 日中で最もスプレッドが広く板が薄い時刻。
ユニバース下限は `min_daily_dollar_volume_usd: 1_000_000`(中央値)。
この帯のマイクロキャップは往復スプレッドが 0.5〜3% になる。

`screening/liquidity.py` の `LiquidityProfile` は実装済みだが、
**バックテストからもモデルからも呼ばれていない**(API表示専用)。

**修正案 D-5**:

1. **スプレッドを追加データ無しで推定する。**
   既存の OHLCV から Corwin–Schultz の高値安値スプレッド推定量を計算する
   (2日分の high/low から相対スプレッドを出す標準的な手法)。
   新規 `src/autoscreener/screening/trading_cost.py`:
   ```python
   def corwin_schultz_spread(bars: list[tuple[float, float]], window: int = 20) -> float | None
   def amihud_illiquidity(returns: list[float], dollar_volumes: list[float]) -> float | None
   def round_trip_cost_bps(spread: float, position_usd: float, adv_usd: float,
                           impact_coefficient: float) -> float
   ```
   マーケットインパクトは `impact = k * sqrt(position / ADV)` の平方根則で十分。
   `k` は新設 `config/execution.yaml` に置く。

2. `runner._realized_return` に `cost_bps` を差し引く経路を追加し、
   `BacktestMetrics` を **コスト前・コスト後の両方**で出す。
   片方だけにすると、次に読む人が必ず取り違える。

3. `/candidates` の各行に `estimated_round_trip_cost_bps` と
   `max_position_usd`(既存の `LiquidityProfile`)を出す。
   **モデル確率が高くてもコストで食われる銘柄を、順位表の上で識別できるようにする。**

**受け入れ基準**: コスト後の `lift_ratio` と `portfolio.excess_cagr`(D-4)が出ること。
コスト前後の差が超過CAGRの半分を超えるなら、`min_daily_dollar_volume_usd` の
再較正が必要という判断材料になる。

---

### D-6【重大】ネットデットを7年間名目一定と仮定している(現金燃焼が終端株主価値に反映されない)

**証拠**: `scoring/moic.py::compute_moic`
```python
terminal_equity = terminal_ev - inputs.net_debt      # net_debt は評価時点の値のまま
```
コメントも「ネットデットは名目で一定と仮定する」と明記している。

**機序と非対称性**: このアプリが狙う典型的な候補は
「ネットキャッシュを持つ、赤字の高成長マイクロキャップ」である。そこでは:

- ネットキャッシュが時価総額の 40〜60% を占めることが珍しくない
  (`net_debt` が大きな負値)。
- モデルは **7年後にもその現金がそのまま株主のものとして残っている**
  として `terminal_equity` に足し込む。実際にはランウェイの範囲で燃え尽きる。
- 逆に FCF 黒字企業が7年間で積む現金は **一切加算されない**。

つまり誤差は **候補の中心プロファイルを systematically 過大評価する向き**に効く。
`cash_runway_quarters` は `health_index` の一成分としてしか使われておらず、
**価値のブリッジには入っていない**。

**なお二重計上に注意**: 燃焼企業は増資で穴を埋めるので、
`dilution_drag` が既に**株数側**を罰している。
今回入れるのは**貸借対照表側**であり、両方が必要(株数が増えても
現金が減れば1株価値は二重に落ちる。それが現実に起きていること)。
ただし「増資で現金が補充される」分を無視すると今度は過小になるので、
**過去の希薄化ペースが将来も続くなら、その調達額は現金に戻す**という形で整合させる。

**修正案 D-6**:

`moic.py` に純粋関数を追加:
```python
def projected_net_debt(inputs: MoicInputs, growth_rates: list[float],
                       terminal_margin: float, dilution_rate: float,
                       config: ScoringConfig) -> float:
    """ホライズン終端のネットデット。
    net_debt_H = net_debt_0
                 - Σ_t (FCF_t)                # 営業からの純増減
                 - Σ_t (増資による調達額)      # dilution_rate × その年の時価総額
    FCF_t は fcf_margin を売上に掛け、fcf_margin は margin と同じ減衰で
    ターミナル値へ寄せる(赤字企業が永久に同率で燃え続ける前提を避ける)。
    """
```
- `config/scoring.yaml` に `balance_sheet:` ブロックを新設。
  `project_net_debt: false`(**既定は無効**)、
  `fcf_margin_fade: 0.75`、`max_net_cash_credit_years: <int>` 等。
- **既定を false にする理由**: D-1/D-2 を直すまで KPI 比較ができないため、
  挙動を変える変更を入れてはいけない。フラグだけ先に入れ、
  母集団が直ってから `compare-configs` で判定する。
- `MoicResult` に `projected_net_debt` と `net_debt_change` を診断値として追加し、
  **無効時でも計算して表示する**(S-5/E-1 と同じ「まず可視化」の手順)。

**受け入れ基準**: 現ランキング上位30銘柄について、
`(projected_net_debt − net_debt) / market_cap` の分布を出す。
中央値が ±5% 以内なら影響は小さい、20%を超える銘柄が3割以上なら重大、と判断する。

---

### D-7【重大】`expected_moic` を対数正規の「平均」と解釈する根拠がない

**証拠**: `moic.py::compute_moic`
```python
mu = math.log(expected_moic) - sigma**2 / 2
```
コメントは「点推定は期待値であって中央値ではない(27.14)」としている。

**問題**: `expected_moic` は5因子の**中心的見通しの積**である。
各因子が対数正規なら、**中央値の積は積の中央値**であって、平均ではない。
平均にするには各因子に `exp(σ_i²/2)` が要るが、それは掛けていない。
したがって `expected_moic` は構造的に**中央値側の量**であり、
そこからさらに `−σ²/2` を引くのは、**根拠のない恒久的な減額**になっている。

**大きさ**: `sigma_shrinkage = 0.85` により σ は断面中心付近(概ね 0.9〜1.1)に
集まる。σ=1.0 なら `exp(−0.5) = 0.61`。**中央値MOICが4割引きされている。**

**なぜ較正で救われないか**: `calibration.py` が較正するのは
「h年でオンペースに乗る確率」だけ。UIの見出しである **P(7年で10倍)は生値のまま**
表示される(モジュール docstring が明示している)。
つまりこの 0.61 倍は利用者が読む数字に直接乗っている。

**順位への影響は小さい**(σ が縮小推定でほぼ定数なので、単調変換に近い)。
**水準への影響は大きい。** そして水準はポジションサイズと期待値の判断に直結する。

**修正案 D-7**:
1. `config/scoring.yaml` の `uncertainty:` に
   `point_estimate_interpretation: "median" | "mean"` を追加。
   `moic.py`:
   ```python
   if unc.point_estimate_interpretation == "median":
       mu = math.log(expected_moic)
   else:
       mu = math.log(expected_moic) - sigma**2 / 2
   ```
2. **既定は現状維持(`mean`)**。D-1/D-2 修正後に `compare-configs` で
   `calibration_error` と `rank_ic` の変化を測って決める。
   予測: 順位KPIはほぼ不変、`calibration_error`(現在 −0.045 = 過小予測)が改善する。
   **その方向に動けば `median` が正しいという実証的な証拠になる。**
3. どちらを採るにせよ、`/candidates` の詳細画面に
   「この確率は log-MOIC が正規分布という仮定の下での値で、実測較正されていない」
   を、較正済みの数字と**視覚的に別扱い**で出す(方針としては既に書かれている。実装を確認すること)。

---

### D-8【中〜重大】価格ナウキャストが22%の観測で上限に張り付き、実質モメンタム加点になっている

**証拠**: `nowcast_cap_hit_rate = 0.2205`(backtest_runs id=28)。
`config/scoring.yaml` の `nowcast_cap = 0.15`。

`moic.py::nowcast_initial_growth` の docstring は
「**これはモメンタム戦略ではない**」と2箇所で主張している。
だが観測の 22% では補正量が上限に固定されており、その銘柄について
ナウキャストが伝えている情報は **12ヶ月超過リターンの符号だけ**、
すなわちモメンタム・ダミー変数そのものである。

**より深刻な問題**: このアプリは **モメンタム単独のベースラインを一度も測っていない。**
モデル全体(成長・粗利率・希薄化・マルチプル圧縮・生存確率・σ縮小)が、
「12ヶ月モメンタムで並べる」に勝っているという証拠がどこにも無い。
`rank_ic = 0.152` は、マイクロキャップの12ヶ月モメンタム単独でも
同程度が出うる水準である。

**修正案 D-8**: `run-backtest` に**ベースライン比較**を必須で組み込む。

`backtest/baselines.py` を新設:
```python
BASELINES = {
    "momentum_12m":        lambda inp, cs, cfg: inp.log_momentum_12m,
    "revenue_growth":      lambda inp, cs, cfg: base_initial_growth(inp, cfg),
    "cheapness":           lambda inp, cs, cfg: -(inp.market_cap + inp.net_debt) / inp.gross_profit_latest,
    "gross_profit_scale":  lambda inp, cs, cfg: inp.gross_profit_latest,
    "random":              lambda inp, cs, cfg: <seeded rng>,
}
```
`_evaluate_one_date` は各 `Observation` に `baseline_scores: dict[str, float]` を持たせ、
`compute_metrics` が **同一観測に対して**各ベースラインの
lift / monotonicity / rank_ic / tail_lift を出す。
`BacktestMetrics.baselines: dict[str, BaselineMetrics]`。

**受け入れ基準**:
- v4 が `momentum_12m` と `revenue_growth` の**両方**に、
  D-2 のブートストラップCIを超える差で勝つこと。
- 勝てないなら、**それが最も重要な発見である**。
  その場合、v4 の複雑さは正当化されない。ナウキャストを外した設定
  (`nowcast_weight: 0.0`)も同じ比較に載せること。

---

### D-9【中】生存確率が完全な事前値で、検証手段がゼロ

`survival.base_annual_hazard = 0.06`, `health_sensitivity = 1.2` は
`config/scoring.yaml` 自身が「(b) 公表された基準率からの事前値」に分類している。
health=0 で7年生存 65% = **全銘柄の確率に 0.65 が掛かる**。
health による差(88% vs 21%)は順位も動かす。

D-1 のとおり標本に廃止が0件なので、**現状このパラメータを検証する手段が無い**。
これは D-1 を直せば自動的に解ける。

**修正案 D-9**(D-1 完了後):
- `scoring/hazard.py` を新設。`health_index` を説明変数に、
  「評価日から1年以内に廃止されたか」を目的変数にしたロジスティック回帰。
  `cli estimate-hazard` で断面ごとに推定し、`base_annual_hazard` と
  `health_sensitivity` の実測値を出す(κ の `estimate-elasticity` と同じ設計)。
- 買収による廃止と破綻による廃止を**分けること**。前者は 10バガーの経路を
  途中で断つが −100% ではない。Form 25 の理由コードと決済価格の符号で分離できる。

---

### D-10【中】ライブとバックテストのゲートが別物 — KPIが適用されない母集団にランキングを出している

**証拠**:

| | ライブ `evaluate_gates` | バックテスト `_passes_point_in_time_gate` |
|---|---|---|
| 時価総額 | `info.marketCap` | 株価 × 株式数 |
| 売上 | `info.totalRevenue`(TTM) | 直近**年次** |
| 株価 | `info.currentPrice` | `price_snapshots.close` |
| キャッシュランウェイ | **適用**(四半期FCF) | **未適用** |
| 上場後期数 | 四半期数 ≥ 4 | **年次期数 ≥ 2** |
| 自己資本マイナス | 適用 | 適用 |

`runner.py` の docstring は「ライブのほうが少しだけ厳しい」と留保しているが、
影響はそれ以上である。`cash_runway_floor`(6四半期未満で除外)は、
**σ と health_index が最も効いている脆弱・高ボラ銘柄群を、ライブでだけ削っている**。
28.6 が「脆弱な企業は上下どちらの裾も厚い」と実測した層そのものである。

**修正案 D-10**:
1. `_passes_point_in_time_gate` を捨て、`evaluate_gates` を**そのまま呼ぶ**。
   `GateInput` をポイントインタイム値で組み立てる新関数
   `point_in_time.build_gate_input(payload, as_of, price, median_dollar_volume)` を作る。
   - キャッシュランウェイは `quarterly_cash_flow` を
     `period_end + REPORTING_LAG_DAYS <= as_of` でフィルタすれば再構成できる。
     **できないと決めつけないこと**(年次と同じ手順で切れる)。
   - `available_quarters` も同じフィルタで数えられる。
2. 再構成が原理的に無理な項目が残ったら、**ライブ側でもその項目を無効にする**。
   検証していないゲートを本番だけで効かせるのは、KPIの主張範囲を壊す。
3. 差分を `BacktestMetrics.gate_parity` として出す:
   評価日ごとに「ライブ相当ゲート通過数 / バックテストゲート通過数」。

---

### D-11【中】価格が配当調整されていない

`yfinance_client.py` は `history(..., auto_adjust=False)` で取得し、
`close` をそのまま `price_snapshots` に保存する。分割は
`snapshot_collector._reconcile_splits` が遡及調整するが、**配当は一切扱っていない**。

影響を受ける箇所:
- `runner._realized_return`(実現リターンが価格リターンのみ)
- `point_in_time.annualized_log_momentum`(ナウキャストの入力)
- `metrics.on_pace_threshold` との突合(閾値は総リターン基準のはず)

マイクロキャップ・グロースでは配当利回りは低いが、
ゲート通過ユニバースには `Real Estate` 除外後も配当を出す成熟企業が混ざる。
**ユニバース基準率(分母)を系統的に押し下げ、リフトを過大に見せる向き**に効く。

**修正案 D-11**:
`price_snapshots` に `dividend numeric` を追加(alembic)。
`fetch_price_and_shares_history` は既に actions 系の列を持っている
(`_recent_splits` が "Stock Splits" 列を見ている)ので、"Dividends" 列を
同じ経路で拾うだけ。`_realized_return` を総リターンに変更する。

---

### D-12【中・現在進行中の運用障害】本番が停止しているのにランキングは更新され続けている

**証拠**(2026-08-28 実測):
```
SELECT is_quarantined, count(*) FROM tickers GROUP BY 1;   -> [(True, 5312)]   ← 全銘柄が隔離
SELECT snapshot_date, count(*) FROM raw_snapshots GROUP BY 1 ORDER BY 1 DESC;
  -> 2026-08-26: 1503   (前日 2026-08-25: 5284)            ← 途中で落ちている
SELECT trade_date, count(*) FROM price_snapshots ...;
  -> 2026-08-26: 1  /  2026-08-25: 1464  /  2026-08-24: 5197
SELECT score_date, count(*) FROM scores ...;
  -> 2026-08-28: 1150 行                                    ← 今日のランキングは存在する
```

**つまり今日(08-28)のランキングは、08-24 の株価と 08-25/26 の財務スナップショットで
作られており、その事実がどこにも表示されていない。**

機序:
- `collect_one` は成功時にしか `is_quarantined = False` にしない。
  `_register_failure` は連続失敗閾値で `True` にする。
  レート制限やネットワーク断で1回の実行が全滅すると、**全銘柄が同時に隔離される**。
- `select_collectable_symbols` は隔離銘柄を `retry_interval_days` 経過後にしか戻さない
  → 復旧が遅延する。
- `monitoring.check_quarantine_health` は **ログにERRORを出すだけ**。
- `run_scoring` にデータ鮮度の前提条件が**無い**。
  `apply_gates` が走れば `universe_snapshots` が今日付で書かれ、
  `run_scoring` はそれを見て今日付の `scores` を書く。
- `/universe/status` は収集ログの件数を返すが、
  **`/candidates` の各行に「この価格は何日前のものか」が無い**。

**修正案 D-12**(4つとも実装すること):

1. **鮮度の前提条件**。`scoring/engine.py::run_scoring` の冒頭で:
   ```python
   max_price_date = session.query(func.max(PriceSnapshot.trade_date)).scalar()
   staleness_days = business_days_between(max_price_date, score_date)
   if staleness_days > config.max_price_staleness_days:   # 既定 2
       logger.error(...); return {"scored": 0, "skipped_reason": "stale_price_data"}
   ```
   さらに「当日ゲート通過銘柄のうち、当日の価格行を持つ割合」が
   閾値(例 0.9)未満なら同様に中止。**中止するほうが、古いランキングを
   新しい日付で出すより安全**である。

2. **鮮度をAPIに出す**。`Score` に `price_as_of date` と
   `financials_as_of date` を追加(`inputs` に相当情報はあるが、
   一覧のフィルタに使うにはカラムが要る)。
   `/candidates` のレスポンスに `data_age_days` を出し、
   UIは 2営業日を超えたら明示的に警告する。

3. **一斉隔離を障害として扱う**。`parallel_runner` に
   「1回の実行での失敗率が 50% を超えたら、その実行で発生した
   `consecutive_failures` の増分をロールバックする」サーキットブレーカーを入れる
   (既にサーキットブレーカーの概念は `parallel_runner.py` にある。拡張する)。
   1銘柄の恒久的失敗と、インフラ障害による全滅は別の事象である。

4. **応急処置(今すぐ)**: バックアップ後に
   `UPDATE tickers SET is_quarantined = false, consecutive_failures = 0;`
   を実行して収集を復旧させること。

---

### D-13〜D-15【小】記録のみ

- **D-13**: `runner._load_payloads` が全評価日で最新 payload を使う。
  リステートメント先読みは docstring に記載済みだが、
  `info.sector` と `_fx_rate_financial_to_trading` の先読みは記載が無い。
  セクターは変更されうる(GICS 再分類)、為替レートは収集時点のスポットである。
  → I-1(XBRL の `filed` 日付)で構造的に解消する。
- **D-14**: `metrics.tail_lifts` の閾値がタイに甘い(`r >= threshold` で
  同値を全部当たりに数える)。低価格帯で 0.00% リターンが密集する日で
  過大になりうる。D-2 の修正に合わせて `>` にするか、閾値を補間する。
- **D-15**: 実質無効化された設定が残っている
  (`size_prior.exponent = 0.0`、`nowcast_cap_sign_flip = 1.0`、
  `multiple.max_change/min_change` が「実質効かない」)。
  害は無いが、将来の担当者が「効いている」と誤読する。
  **無効であることを値ではなくコメントの先頭行で宣言する**か、削除する。

---

## 2. プロの投資家に勝つために必要な情報

### 2.1 まず「どこで勝つのか」を確定させる

投資の優位は3種類しかない。順に潰していく。

**(a) 情報優位 — 現状のアプリは構造的に負けている。**
モデルの入力は年次財務諸表と株価だけである
(`point_in_time.py` の設計判断。バックテスト可能性のために正しい選択だった)。
`REPORTING_LAG_DAYS = 90` により、モデルは最悪 **15ヶ月前の事業実態**から
7年後を外挿している。プロは四半期決算・カンファレンスコール・
ガイダンス・セグメント別・エキスパートネットワーク・オルタナティブデータを見ている。
**この土俵では絶対に勝てない。**

ただし **これは修正可能である**。後述の I-1 で、開示ラグの近似も四半期の欠落も
両方消える。「四半期粒度で、実際の提出日で、2009年まで遡れる」データが
**無料で存在する**のに使っていない、というのが現状の最大の情報欠落である。

**(b) 分析優位 — 引き分けが限界。**
15.1 の恒等式分解(売上 × 利益率 × マルチプル ÷ 株数)は正しいが、
プロが50年やってきたことでもある。κ の断面推定、σ の縮小推定、
Vasicek コピュラによるポートフォリオ確率は、いずれも実装の質は高いが
**発想として新規性は無い**。ここで優位は生まれない。

**(c) 構造優位 — 個人が勝てる唯一の場所。そして今アプリが1つも測っていないもの。**

機関投資家が構造的にできないこと:
- 時価総額 $300M 未満、ADV $1M 未満の銘柄を**意味のある規模で**買えない
  ($1B のファンドが 1% 建てるには $10M = ADV の10日分)
- セルサイドのカバレッジが 0〜1 の銘柄をリサーチする経済合理性が無い
- 3年負け続ける戦略を続けられない(**キャリアリスク**)。7年ホライズンは
  ファンドの評価サイクルと構造的に不整合である
- 四半期のトラッキングエラーに縛られ、指数から大きく外れた集中を取れない

**このアプリのホライズン設定(7年)とサイズ帯(マイクロキャップ)は、
偶然にもこの構造優位のど真ん中にある。** ところが:

> **「その銘柄がどれだけ無視されているか」を測る指標が、アプリに1つも存在しない。**

`financial_metrics.py` は 27.16 で「アナリストカバレッジ」「機関保有率」
「インサイダー買い越し」を**削除**している。理由は「過去に遡れないから
バックテストが成立しなくなる」であり、**yfinance 経由では正しい判断**だった。
しかし **SEC EDGAR 経由なら全部ポイントインタイムで取れる。**
削除の理由がデータ源の制約であって指標の無価値さではなかった以上、
データ源を変えれば復活させるべきである。

### 2.2 追加すべきデータ源(効果 ÷ 実装コスト順)

#### I-1【最優先】SEC XBRL companyfacts の `filed` 日付による真のポイントインタイム化

**なぜ最優先か**: 単独で D-2(標本不足)、D-13(先読み)、
D-10(ゲートの非対称)、そして (a) の情報劣位を**同時に**解決する。

**現状**: `validation/xbrl_facts.py` は既に companyfacts を叩いているが、
抽出しているのは **4概念だけ**(revenue / cash / liabilities / shares_outstanding)で、
用途は `validation/reconciliation.py`(yfinance との突合)に限られている。
**モデルは1バイトも使っていない。**

**companyfacts の各 fact が持つフィールド**:
`start`, `end`, `val`, `accn`, `fy`, `fp`, `form`, `filed`, `frame`

`filed` が**実際の提出日**である。`filed <= as_of` でフィルタするだけで:
- `REPORTING_LAG_DAYS = 90` の近似が**不要になる**(早期提出企業に保守的すぎ、
  遅延企業に楽観的すぎるという既知の歪みが消える)
- **リステートメントの先読みが消える**。同じ `end` に複数の `filed` がある場合、
  `filed <= as_of` の中で最も早いものが「当時開示されていた値そのもの」である。
  これは `point_in_time.py` の docstring が「限界1」として挙げている問題の**完全な解決**である
- **四半期粒度**が手に入る(`fp` = Q1/Q2/Q3/FY)。年次のみという制約が消える
- **履歴が2009年まで遡れる**。yfinance の年次5期(13.1)という制約が消える
  → 評価日が 8点 から **60点以上**に増え、D-2 の検出力問題が実質的に解決する

**実装**:

1. `validation/xbrl_facts.py::CONCEPT_TAGS` を大幅拡張する。
   最低限必要なのは、`MoicInputs` の全フィールドを埋められる集合:

   | 概念 | タグ候補(優先順) |
   |---|---|
   | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, `SalesRevenueNet` |
   | cost_of_revenue | `CostOfRevenue`, `CostOfGoodsAndServicesSold` |
   | gross_profit | `GrossProfit`(無ければ revenue − cost_of_revenue) |
   | net_income | `NetIncomeLoss` |
   | operating_cash_flow | `NetCashProvidedByUsedInOperatingActivities` |
   | capex | `PaymentsToAcquirePropertyPlantAndEquipment` |
   | assets | `Assets` |
   | liabilities | `Liabilities` |
   | equity | `StockholdersEquity` |
   | cash | `CashAndCashEquivalentsAtCarryingValue` + `ShortTermInvestments` |
   | debt | `LongTermDebtNoncurrent` + `LongTermDebtCurrent` + `ShortTermBorrowings` |
   | **lease_liability** | `OperatingLeaseLiabilityNoncurrent` + `OperatingLeaseLiabilityCurrent` |
   | current_assets / current_liabilities | `AssetsCurrent` / `LiabilitiesCurrent` |
   | shares_outstanding | `dei:EntityCommonStockSharesOutstanding` |
   | diluted_shares | `WeightedAverageNumberOfDilutedSharesOutstanding` |
   | **public_float** | `dei:EntityPublicFloat` ← **I-4 参照。構造優位の直接測定** |
   | r_and_d | `ResearchAndDevelopmentExpense` |

   **`lease_liability` が単独で取れる**点に注意。
   これは既知の S-5(`Total Debt` にオペレーティングリースが混入し
   `leverage_effect` を歪める)を、診断フラグではなく**計算の修正**として解決する。

2. 新規 `scoring/point_in_time_xbrl.py`:
   `build_moic_inputs_from_xbrl(cik, as_of, price_observations, share_observations)`
   — 既存の `build_moic_inputs` と**同じ `MoicInputs` を返す**契約にする。
   モデル本体(`moic.py`)は一切変更しない。

3. `config/scoring.yaml` に `data_source: "yfinance" | "xbrl"` を追加し、
   `engine.run_scoring` と `backtest.runner` の両方で切り替え可能にする。
   **両方で同じKPIを出して比較する**のが移行の受け入れ基準。

4. `backfill_history` を `period="max"` に変更(現状3年)。
   companyfacts が2009年まであっても、価格が3年しか無ければ意味がない。

**受け入れ基準**:
- `rebalance_dates` が 40点以上返ること(interval 91日、非重複モードでも 10点以上)
- 同一評価日・同一銘柄で yfinance 経由と XBRL 経由の `MoicInputs` を突合し、
  乖離の分布を出す(`validation/reconciliation.py` の既存の枠組みを使う)
- **D-2 のブートストラップCIが、現状の 1/3 以下に縮むこと**

**コスト**: SEC のレート制限は 10 req/s。5,300銘柄 × 1 companyfacts = 約9分。
`edgar_client.RateLimiter` が既にある。週次で十分(30.5.5 と同じサイクル)。

---

#### I-2【最優先】上場廃止ユニバース(D-1 の修正そのもの)

上の D-1 修正案を参照。**I-1 と同じ EDGAR full-index 走査で実装できる**ので、
1つの作業としてまとめること。

---

#### I-3【高】Form 4(インサイダー売買)— 完全にポイントインタイム

**なぜ効くか**: 経営陣は自社の四半期先を最もよく知っている。
小型株でのインサイダー買いは、公開情報の中で最も頑健に文書化された
リターン予測因子の1つである。そして **プロのファンドはマイクロキャップの
Form 4 を体系的に使いにくい**(規模制約とコンプライアンス)。
構造優位そのものである。

**入手**: EDGAR full-index に `4` として全件出る。本体は XML
(`ownershipDocument`)で機械可読。取得できる情報:
- `transactionCode`(P = 市場買付、S = 売却、A = 付与、M = オプション行使)
- `transactionShares`, `transactionPricePerShare`, `transactionDate`
- `sharesOwnedFollowingTransaction`(取引後保有株数)
- `isDirector` / `isOfficer` / `isTenPercentOwner`, `officerTitle`
- **提出日 = 公開日**(取引から2営業日以内が義務)→ 先読みが原理的に起きない

**モデルへの入れ方**(手順を守ること):
1. まず `MoicInputs` に **入れない**。`Observation` の診断フィールドとして保存し、
   `run-backtest` で **モデル確率の上位デシルに条件付けた順位IC**を測る。
   これは 28.10(Piotroski を fade に載せた)と**まったく同じ手続き**であり、
   既に成功実績のある方法である。
2. IC が有意なら、入れる場所を選ぶ。候補:
   - `growth_fade`(Piotroski と同じ経路。持続性の代理)
   - 独立した項として `mu` に加算
   - `health_index` に入れるのは**間違い**。インサイダー買いは
     生存確率ではなくリターンの情報である
3. 指標の定義は「直近6ヶ月の net open-market buying(P − S)を、
   時価総額または取引後保有株数で正規化」。**金額ではなく比率**にする
   (絶対額は時価総額と相関してしまう)。

---

#### I-4【高】浮動株比率とインサイダー保有 — `dei:EntityPublicFloat`

**I-1 に含まれるので実質ゼロコスト。** これが「構造優位の直接測定」になる。

`dei:EntityPublicFloat` は 10-K/10-Q のカバーページに XBRL タグとして
入っている(SEC が要求している)。時価総額と組み合わせて:

```
insider_and_affiliate_ownership ≈ 1 − EntityPublicFloat / market_cap_at_measurement_date
```

(`EntityPublicFloat` は測定日が別に入っているので、その日の時価総額と比べること)

**なぜ 10バガー探索に直接効くか**:
- 創業者・経営陣の持株比率が高い企業は、短期の希薄化に対する規律が働く
  (15.1④ が「単独で最大の改善余地」とした軸そのもの)
- 浮動株が小さい = 機関が入れない = **構造優位が存在する条件**
- 同時にリスクでもある(流動性・ガバナンス)。だから**加点ではなく、
  「構造優位が存在する母集団かどうか」の層別に使う**

**まずやること**: モデルに入れる前に、`run-backtest` で
**浮動株比率の五分位別に KPI を出す**。
モデルのリフトが「浮動株が小さい層でだけ立っている」なら、それが
このアプリの edge の正体であり、ユニバース定義をそちらへ寄せるべきである。
逆に浮動株の大きい層でリフトが立っているなら、**プロと同じ土俵で戦っており、
edge の主張は成立しない**。

**この1つの分析が、「プロに勝てるか」という問いに対する最も直接的な答えになる。**

---

#### I-5【中】FINRA 空売り残高

半月ごとの全銘柄空売り残高が無料・機械可読で公開されている
(consolidated short interest ファイル)。
`days_to_cover = short_interest / avg_daily_volume`。
マイクロキャップでは (a) バリュートラップの検知、(b) スクイーズの上振れ、
両方の情報を持つ。実装は `fred_client` と同型で軽い。
**優先度は I-1〜I-4 の後。** 単独では edge にならない。

---

#### I-6【中】13F(機関投資家保有)

I-4 の浮動株比率で代替できるなら不要。より精密にやるなら:
EDGAR full-index の `13F-HR` → XML の `infoTable` は **CUSIP** で銘柄を持つ。
CUSIP → ティッカーの解決が必要で、SEC が四半期ごとに公開している
「13F list of securities」(CUSIP + issuer name)を使う。手間は大きい。
**I-4 で効果が確認できてから着手すること。**

---

#### I-7【中】取引コストの実データ推定(D-5 の材料)

**追加データ取得は不要**。既存の OHLCV から:
- Corwin–Schultz 高値安値スプレッド推定量 → 実効スプレッド
- Amihud 非流動性 = mean(|return| / dollar_volume) → マーケットインパクト係数

これで D-5 のコストモデルを、仮定ではなく実測で埋められる。

---

### 2.3 情報以外に足りないもの

**ポートフォリオを一度も組んでいない**(D-4)。
「上位20銘柄のうち1つでも10倍になる確率」は `scoring/portfolio.py` が
Vasicek コピュラで計算しているが、それは**モデル確率の合成**であって、
**実際に建てたらどうだったか**ではない。
`config/portfolio.yaml` の規律(30銘柄・4%上限・セクター25%上限)を
バックテストで一度も適用していない。

**売却規律が実装されていない**。
`monitoring_metrics.py` は四半期の点灯を出すが、
「点灯した銘柄をポートフォリオから外したらリターンはどう変わったか」を
測る仕組みが無い。7年ホライズンで**入口だけを最適化して出口を測っていない**のは、
10バガー戦略として片肺である。

**再現性の記録が無い**。
`config/scoring.yaml` のコメントに書かれた実測値は、どの `backtest_runs.id` の
どの実行から来たのかが辿れない。`compare-configs`(D-2 修正案)の出力を
そのまま貼る運用にすれば解決する。

---

## 3. 実装順序(この順を守ること)

**フェーズ A — 土台を直す(これが終わるまでモデルに触らないこと)**

| # | 作業 | 対応する欠陥 |
|---|---|---|
| A-1 | 本番の一斉隔離を復旧、鮮度ガードとサーキットブレーカーを実装 | D-12 |
| A-2 | ベンチマーク登録 + ポートフォリオ・シミュレーション | D-4 |
| A-3 | 取引コストモデル(Corwin–Schultz + Amihud) | D-5, I-7 |
| A-4 | 観測数加重KPI + ブートストラップCI + 非重複モード + `compare-configs` | D-2, D-14 |
| A-5 | KPI の PASS/FAIL 判定 | D-3 |

A-5 が終わった時点で、**現行 v4 が本当に何を達成しているのか**が初めて分かる。

**フェーズ B — 標本を直す**

| # | 作業 | 対応 |
|---|---|---|
| B-1 | EDGAR full-index 走査基盤(I-1 と I-2 で共用) | — |
| B-2 | 上場廃止ユニバース構築 + 価格バックフィル | D-1 |
| B-3 | XBRL companyfacts のポイントインタイム化(`filed` 基準) | I-1, D-13 |
| B-4 | 価格履歴を `period="max"` へ拡張 | I-1 |
| B-5 | ゲートをライブ/バックテストで統一 | D-10 |
| B-6 | 配当を含む総リターンへ | D-11 |
| B-7 | ハザード率の実測較正 | D-9 |

B が終わると、評価日が 8 → 40+ に増え、廃止銘柄が入り、
**A-4 の信頼区間が実用的な幅に縮む**。ここで初めてモデルの議論ができる。

**フェーズ C — モデルを検証し直す**

| # | 作業 | 対応 |
|---|---|---|
| C-1 | ベースライン比較(モメンタム / 成長率 / 割安 / ランダム) | D-8 |
| C-2 | `config/scoring.yaml` の (c) 分類パラメータを `compare-configs` で全部再判定 | D-2 |
| C-3 | `point_estimate_interpretation` を実測で決める | D-7 |
| C-4 | ネットデットの射影を実測で判定 | D-6 |
| C-5 | 浮動株比率の五分位別 KPI(**edge の存否を判定する分析**) | I-4 |

**フェーズ D — 情報を足す**

| # | 作業 | 対応 |
|---|---|---|
| D-a | Form 4 収集 + 条件付き順位IC 測定 | I-3 |
| D-b | 効果が確認できたものだけモデルへ | — |
| D-c | FINRA 空売り残高 | I-5 |
| D-d | 13F(I-4 で不足なら) | I-6 |

---

## 4. やってはいけないこと

1. **現在の8評価日・生存者標本の上で、新しいパラメータを追加・調整しないこと。**
   `config/scoring.yaml` 冒頭の「変更したら必ず `run-backtest` で確認」という
   方針は正しいが、**今の `run-backtest` は確認になっていない**。
   フェーズ A-4 と B が終わるまで、KPI の小数第2位の比較で意思決定しないこと。

2. **欠陥の修正で KPI が改善することを期待しないこと。**
   D-1(廃止銘柄の投入)は確実に KPI を悪化させる。
   D-5(コスト)も悪化させる。**それが正しい結果である。**
   悪化しなかったら実装を疑うこと。

3. **診断フラグを増やして満足しないこと。**
   S-5(リース)、S-6(成長率クランプ)、A-1(希薄化欠損)、E-1(net_debt欠損)は
   いずれも「まず可視化する」で止まっている。手続きとしては正しいが、
   **可視化したまま放置すると、フラグが立ったまま使われ続けるだけ**になる。
   各フラグに「いつ、どのKPIで、採否を判断するか」を書くこと。

4. **モデルを複雑にする前に、単純なベースラインに勝つことを証明すること**(D-8)。
   v4 は約1,000行のモデルである。12ヶ月モメンタムは1行である。
   後者に勝てないなら、前者の存在理由が無い。

5. **「プロに勝つ」の意味を、リフト倍率で語らないこと。**
   ユニバース内相対のリフトは、プロとの比較ではない。
   D-4 のベンチマーク超過CAGRと最大ドローダウンが出るまで、
   その主張は数字の裏づけを持たない。

---

## 付録: 本監査で使った実データクエリ

```sql
-- 生存バイアスの確認(D-1)
SELECT count(*) FROM tickers;                                    -- 5312
SELECT count(*) FROM tickers WHERE delisted_at IS NOT NULL;      -- 0
SELECT count(*) FROM tickers WHERE is_quarantined;               -- 5312  ← D-12
SELECT min(trade_date), max(trade_date), count(*) FROM price_snapshots;
  -- 2023-08-22 .. 2026-08-26, 3,567,278行
SELECT bucket, count(*) FROM (
  SELECT ticker_id,
         CASE WHEN max(trade_date) >= DATE '2026-08-20' THEN 'active' ELSE 'stopped' END AS bucket
  FROM price_snapshots GROUP BY ticker_id) t GROUP BY 1;         -- active: 5286 のみ

-- 運用停止の確認(D-12)
SELECT snapshot_date, count(*) FROM raw_snapshots GROUP BY 1 ORDER BY 1 DESC LIMIT 4;
  -- 2026-08-26:1503 / 08-25:5284 / 08-24:4340 / 08-23:5166
SELECT trade_date, count(*) FROM price_snapshots GROUP BY 1 ORDER BY 1 DESC LIMIT 4;
  -- 2026-08-26:1 / 08-25:1464 / 08-24:5197 / 08-21:5285
SELECT score_date, count(*), count(probability) FROM scores GROUP BY 1 ORDER BY 1 DESC LIMIT 3;
  -- 2026-08-28: 1150 / 766

-- 検証資産の空(D-1, D-9)
SELECT count(*) FROM forward_returns;                            -- 0

-- 最新バックテスト(D-2, D-3, D-8) : backtest_runs id=28, horizon 365d, 3,107観測
--   asset_correlation      0.0563
--   universe_on_pace_rate  0.2481    lift_ratio            1.5048
--   decile_monotonicity    0.8061    rank_ic               0.1521
--   rank_ic_t_stat         3.4821    lift_ratio_worst_date 1.0251
--   delisted_settlement_rate 0.0     calibration_error    -0.0455
--   universe_loss_rate     0.1072    top_decile_loss_rate  0.1044
--   nowcast_cap_hit_rate   0.2205
```
