# v4モデル監査と修正計画（引き継ぎ文書）

| | |
|---|---|
| 作成日 | 2026-08-26 |
| 監査対象 | `scoring_version: v4` / スコア基準日 **2026-08-25**（v4はこの1日分しか存在しない。→ B-1） |
| 対象コード | `src/autoscreener/scoring/moic.py`, `point_in_time.py`, `engine.py`, `config/scoring.yaml` |
| 上位互換 | 要件定義書 `10bagger_app_requirements.md` 27章・28章の続き。本文書は28章以降の実測監査にあたる |

---

> ## 📌 後日の追記(2026-08-26 夜):本文書の一部の記述は事実と違っていた
>
> 本文書とは独立に、コードを直接読み直す欠陥調査を行った結果、
> **ここで「修正済み」と記録されている項目のうち2つが、実際には効いていなかった**
> ことが分かった。追跡と修正は [`defect_fixes_2026-08-26.md`](defect_fixes_2026-08-26.md) にある。
>
> | 本文書の記述 | 実際 | 修正 |
> |---|---|---|
> | **S-1**:粗利率フロアが加点装置になる問題を修正した | **前期粗利率が取れない銘柄が通る経路には修正が入っていなかった**(`terminal_gross_margin` の early return) | D-2 |
> | **S-7**:観測が1つしかない銘柄に保守的な成長率上限を適用する | **`config/scoring.yaml` に値が無く既定 1.0 で無効**。さらに価格ナウキャストが上限を迂回していた。動機として挙げられた BRUN 型は総合9位に居座っていた | D-5 |
>
> **S-7 のテスト(`test_single_observation_growth_uses_a_more_conservative_ceiling`)は
> 設定値そのものを閾値にしていたため、機構が存在しなくても緑になる書き方だった**
> (D-6)。本文書の受け入れ基準を「テストが通ること」で満たす場合は、
> **そのテストが失敗しうる形になっているか**を必ず確認すること。
>
> あわせて、本文書が基準値として記録している KPI(§2.2 のデシル単調性・リフト倍率)は
> **評価日をまたいでデシルをプールしていた指標側の欠陥**(D-4)を含んだ値である。
> 修正後の値は `defect_fixes_2026-08-26.md` を参照すること。

---

## 0. この文書の目的と使い方

### 0.1 目的

v4モデルのランキング上位銘柄を実データで逆解析した結果、**「クランプ（上限・下限）に当たった銘柄が上位に来る」という構造的な欠陥**が見つかった。本文書はその全項目・原因・修正案・実装手順を、担当者が代わっても作業を継続できる粒度で記録する。

### 0.2 引き継ぐ担当者（AIエージェント含む）への指示

1. **最初に §2 の再現コマンドを実行し、本文書の数字が今も再現するか確認すること。** 日次パイプラインが回っているため、日が経つと数字は動く。再現しない場合は §2.3 の手順で数字を取り直してから着手する。
2. **§5 の実装順序に従うこと。** 特に `B-1`（v4スコアのバックフィル）を先に済ませないと、他の修正の効果を測る基準線が存在しない。
3. **`config/scoring.yaml` を1文字でも変えたら `config_hash` が変わり、確率の較正写像が無効化される。** 必ず `run-backtest` → `run-scoring` の順で再実行する（README「トラブルシューティング」参照）。
4. **APIプロセスは自動で設定を読み直さない**（`--reload` なしの場合）。スキーマを変えたら必ず再起動する。
5. モデルの挙動を変える修正（S-2/S-3/S-4/S-9）は **`run-backtest` でKPIの変化を確認してからコミットすること。** 悪化したら戻す。KPIの基準値は §2.2 に記録してある。
6. 本文書の各項目には **受け入れ基準** を書いてある。それを満たしたら §9 のチェックボックスを埋め、この文書自体を更新すること。

### 0.3 やってはいけないこと

- **リターンにフィットさせてパラメータを選ばない。** `multiple.growth_elasticity`（κ）は断面から測った構造パラメータであり、`estimate-elasticity` で測り直すもの。KPIが上がるからといって手で動かさない（要件定義書28.2）。
- **欠損を減点に読み替えない**（27.1の方針）。ただし本監査で判明したとおり、**欠損を満点に読み替えている箇所が実在する**（A-1）。これは別問題として直す。
- **`min_expected_moic` を下げない。** 対数正規モデルは期待値を固定したまま分散を広げると閾値超過確率を上げるため、この下限が外れると「モデルが外れることに賭ける」順位づけになる（27.17）。

---

## 1. 要旨

### 1.1 一行で

**v4のランキング上位は、モデルの外挿限界（クランプ）に当たった銘柄を優先的に選んでいる。** 上位30銘柄と残り371銘柄で、クランプ到達率・入力欠損率が桁違いに違う。

### 1.2 証拠（2026-08-25、ランキング401銘柄）

| 現象 | 上位30位 | 31〜100位 | 101位以下 |
|---|---|---|---|
| 成長率が上限60%に張り付き | **17%** | 1% | 1% |
| 価格ナウキャストが±15ptの上限に張り付き | 23% | **30%** | 21% |
| 粗利率フロア(5%)で押し上げ | **7%** | 0% | 1% |
| `margin_multiple ≥ 2.0` | **10%** | 1% | 0% |
| `revenue_growth_volatility` 欠損（既定0.30を使用） | **17%** | 10% | 4% |
| `dilution_cagr` 欠損（**希薄化ゼロ扱い**） | **7%** | 1% | 0% |
| `leverage_effect ≥ 1.5` | **17%** | 0% | 0% |

### 1.3 順位が実際に何で決まっているか（順位相関、n=401）

| 因子 | 順位相関 |
|---|---|
| **expected_moic（中心的見通し）** | **+0.982** |
| 売上倍率 | +0.444 |
| 初期成長率 g0 | +0.431 |
| マルチプル変化 | −0.426（g0と機械的に連動） |
| 粗利率倍率 | +0.340 |
| σ | +0.253 |
| レバレッジ倍率 | +0.249 |
| EV/粗利 | +0.187 |
| 希薄化 | −0.137 |
| **生存確率** | **+0.031** |
| **時価総額** | **−0.013** |

読み方：`sigma_shrinkage: 0.85` により σ の銘柄差がほぼ潰れているため、**「P(10倍)」という商品名にもかかわらず、順位は実質的にリスク未調整の期待倍率の順序**になっている。生存確率は順位をほとんど動かしていない（KOS は生存確率33%で18位）。

---

## 2. 監査の再現方法

### 2.1 前提

```bash
docker compose up -d --wait                                      # Postgres
uv run uvicorn autoscreener.api.main:app --port 8000 --reload    # API（別ターミナル）
```

DBに直接SQLを投げるのが最も速い。以下すべて `docker compose exec -T db psql -U autoscreener -d autoscreener` 経由。

### 2.2 監査時点のKPI基準線（`GET /api/v1/backtest/latest`、2026-08-25 23:04 実行）

モデルを変更したら `run-backtest` を再実行し、この表と比べること。

| KPI | 基準値 |
|---|---|
| horizon | 0.9993 年（**7年ではない**） |
| observation_count | 3,003 |
| decile_monotonicity | **0.8303** |
| lift_ratio（オンペース率のリフト） | **1.2719** |
| lift_ratio_worst_date | **0.8951**（＝最悪日は負けている） |
| rank_ic | **+0.14794**（t = 3.2035） |
| universe_on_pace_rate | 0.25075 |
| top_decile_loss_rate | 0.10631 |
| universe_loss_rate | 0.10856 |
| calibration_error | −0.05024 |
| delisted_settlement_rate | **0.0000**（生存バイアスの警告灯） |
| asset_correlation | 0.05708 |

### 2.3 数字を取り直すSQL

**(a) 帯ごとのクランプ到達率・欠損率（§1.2の再現）**

```sql
WITH r AS (
  SELECT t.symbol, ROW_NUMBER() OVER (ORDER BY s.probability DESC) rk,
    (s.factors->>'initial_growth_rate')::float g0,
    (s.factors->>'growth_nowcast_adjustment')::float nc,
    (s.factors->>'margin_multiple')::float mar,
    (s.inputs->>'gross_margin_latest')::float gm,
    (s.factors->>'terminal_gross_margin')::float gmt,
    (s.factors->>'leverage_effect')::float lev,
    s.inputs->>'revenue_growth_volatility' gvol,
    s.inputs->>'dilution_cagr' dilc,
    s.inputs->>'revenue_cagr' cagr, s.inputs->>'revenue_yoy' yoy
  FROM scores s JOIN tickers t ON t.id = s.ticker_id
  WHERE s.score_date = (SELECT max(score_date) FROM scores WHERE scoring_version='v4')
    AND s.scoring_version = 'v4' AND s.probability IS NOT NULL)
SELECT CASE WHEN rk<=30 THEN 'A_top30' WHEN rk<=100 THEN 'B_31-100' ELSE 'C_rest' END band,
  count(*),
  round(100.0*avg((g0>=0.599)::int),0)                   AS pct_growth_cap,
  round(100.0*avg((abs(nc)>=0.1499)::int),0)             AS pct_nowcast_cap,
  round(100.0*avg((gmt<=0.0501 AND gm<0.05)::int),0)     AS pct_margin_floor,
  round(100.0*avg((mar>=2.0)::int),0)                    AS pct_margin_ge2,
  round(100.0*avg((gvol IS NULL)::int),0)                AS pct_gvol_missing,
  round(100.0*avg((dilc IS NULL)::int),0)                AS pct_dilution_missing,
  round(100.0*avg((cagr IS NULL OR yoy IS NULL)::int),0) AS pct_single_growth_obs,
  round(100.0*avg((lev>=1.5)::int),0)                    AS pct_lev_ge15
FROM r GROUP BY 1 ORDER BY 1;
```

**(b) 順位相関（§1.3の再現）**

```sql
WITH r AS (
  SELECT s.probability::float p, s.log_moic_sigma::float sig, s.survival_probability::float sv,
    (s.factors->>'leverage_effect')::float lev,(s.factors->>'revenue_multiple')::float rev,
    (s.factors->>'margin_multiple')::float mar,(s.factors->>'multiple_change')::float mch,
    (s.factors->>'dilution_drag')::float dil,(s.factors->>'current_ev_to_gross_profit')::float evgp,
    (s.factors->>'initial_growth_rate')::float g0,(s.factors->>'expected_moic')::float em,
    (s.inputs->>'market_cap')::float mc
  FROM scores s WHERE s.score_date=(SELECT max(score_date) FROM scores WHERE scoring_version='v4')
    AND s.scoring_version='v4' AND s.probability IS NOT NULL),
q AS (SELECT rank() OVER (ORDER BY p) rp, rank() OVER (ORDER BY lev) a, rank() OVER (ORDER BY rev) b,
  rank() OVER (ORDER BY mar) c, rank() OVER (ORDER BY mch) d, rank() OVER (ORDER BY dil) e,
  rank() OVER (ORDER BY evgp) f, rank() OVER (ORDER BY g0) g, rank() OVER (ORDER BY sig) h,
  rank() OVER (ORDER BY sv) i, rank() OVER (ORDER BY mc) j, rank() OVER (ORDER BY em) k FROM r)
SELECT round(corr(rp,a)::numeric,3) lev, round(corr(rp,b)::numeric,3) rev,
  round(corr(rp,c)::numeric,3) margin, round(corr(rp,d)::numeric,3) mult_chg,
  round(corr(rp,e)::numeric,3) dilution, round(corr(rp,f)::numeric,3) ev_gp,
  round(corr(rp,g)::numeric,3) g0, round(corr(rp,h)::numeric,3) sigma,
  round(corr(rp,i)::numeric,3) surv, round(corr(rp,j)::numeric,3) mcap,
  round(corr(rp,k)::numeric,3) exp_moic FROM q;
```

**(c) 粗利率フロアに当たっている銘柄の特定（S-1の再現）**

```sql
WITH r AS (
  SELECT t.symbol, ROW_NUMBER() OVER (ORDER BY s.probability DESC) rk,
    (s.inputs->>'gross_margin_latest')::float gm,(s.inputs->>'gross_margin_prior')::float gmp,
    (s.factors->>'terminal_gross_margin')::float gmt,(s.factors->>'margin_multiple')::float mm
  FROM scores s JOIN tickers t ON t.id=s.ticker_id
  WHERE s.score_date=(SELECT max(score_date) FROM scores WHERE scoring_version='v4')
    AND s.scoring_version='v4' AND s.probability IS NOT NULL),
c AS (SELECT *, GREATEST(LEAST((gm-gmp)*0.40*7, 0.15), -0.15) AS total_change FROM r WHERE gmp IS NOT NULL)
SELECT symbol, rk, round(gm::numeric,4) gm, round(gmp::numeric,4) gm_prior,
  round((gm+total_change)::numeric,4) raw_terminal, round(gmt::numeric,4) gmt, round(mm::numeric,2) mm
FROM c WHERE gm+total_change < 0.05 ORDER BY rk;
```

**(d) 上位銘柄の確率水準(§11.2の教訓。モデル変更のたびに必ず確認)**

順位指標(単調性・リフト・rank_IC)は順位ベースなので、確率の**絶対水準**が
壊れても検出できない。READMEは「上位銘柄でも数%」と明記しているので、
最大値が二桁%になっていたらモデル側の異常を疑うこと。

```sql
SELECT count(*),
  round((max(probability)*100)::numeric, 2)  AS max_pct,
  round((avg(probability) FILTER (WHERE rk <= 5)*100)::numeric, 2) AS top5_avg_pct,
  round((percentile_cont(0.5) WITHIN GROUP (ORDER BY probability)*100)::numeric, 4) AS median_pct
FROM (
  SELECT probability, ROW_NUMBER() OVER (ORDER BY probability DESC) rk
  FROM scores
  WHERE score_date = (SELECT max(score_date) FROM scores WHERE scoring_version='v4')
    AND scoring_version = 'v4' AND probability IS NOT NULL
) x;
```

基準値(2026-08-26、採用済みの修正をすべて適用した状態):
`max_pct = 7.58` / `top5_avg_pct = 5.00` / `median_pct = 0.0895`

---

## 3. 修正項目一覧

| ID | 深刻度 | 項目 | 影響（2026-08-25時点） | 工数 | backtest再実行 |
|---|---|---|---|---|---|
| **S-1** | 致命 | 粗利率 `floor` が利益率崩壊を4.29倍の改善に反転 | 4銘柄。**AMR 6位・KOS 18位** | XS（1行） | 必要 |
| **S-2** | 高 | 粗利率上限が絶対値(15pt)で、薄利企業ほど有利 | `mm≥1.5` 22件中**12件が上位40位** | S | **必須** |
| **S-3** | 中 | 粗利率トレンドが直近2期の差分1回のみ | 上記と同根 | M | **必須** |
| **S-4** | 中 | 利益率の谷にある銘柄でEV/粗利据え置きが逆向きに効く | AMR(EV/GP 93.9)・VMET(98.9) | M | **必須** |
| **S-5** | 高 | `net_debt` にリース債務が入り、小売・外食が上位に | **DBI 3位**・SMC 16位・KOS 18位・CODI 28位 | M | 必要 |
| **S-6** | 中 | 成長率上限60%への張り付きが上位に集中し順位が決まらない | 上位30の**17%** | S | 段階2のみ必要 |
| **S-7** | 中 | `min(CAGR,YoY)` の安全装置が片方欠損だと無効化 | BRUN 8位 | S | 必要 |
| **S-8** | 中 | ナウキャストが上限張り付きで実質モメンタム加点 | 31〜100位の**30%** | S | **必須** |
| **S-9** | 中 | σの85%縮小でリスク項が順位から消えている | 生存確率の順位相関 +0.031 | S | 案2のみ必要 |
| **A-1** | 中 | 希薄化の欠損を「希薄化ゼロ」として扱う | 上位30の7% | XS | 必要 |
| **A-2** | 低 | 売上$27〜35Mの企業がEV/粗利50〜99倍で上位 | VMET 1位・BRUN 8位 | S | 必要 |
| **B-1** | 高 | **v4のスコアが1日分しかない** | `/rank-changes` がv4で機能しない | XS | 不要 |
| **B-2** | 低 | 検証は1年、商品は7年 | 表示のみ | XS | 不要 |
| **B-3** | 低 | 較正済み確率の有効数字が実態を超えている | 上位30が全部31%台 | XS | 不要 |
| **B-4** | 低 | 最悪日リフト0.895がランキング画面に出ていない | 表示のみ | XS | 不要 |
| **B-5** | 中 | `delisted_at` が5,312銘柄中**0件** | 生存バイアスが将来も解消しない | M | 不要 |
| **B-6** | 低 | `/universe/status` が実行途中の数字を出す | 誤読の原因 | S | 不要 |
| **B-7** | 低 | "invalid_data" ラベルが除外を意味するように見える | 表示のみ | XS | 不要 |
| **B-8** | 低 | ユニバースから消えた銘柄が `tickers` に残り続ける | 24銘柄が毎日404 | S | 不要 |
| **C-1** | 中 | 下振れ確率が画面に無い | 機能追加 | XS | 不要 |
| **C-2** | 低 | EV/粗利の自社ヒストリーが無い | 機能追加 | M | 不要 |
| **C-3** | 低 | 建てられるサイズ（流動性の実額）が無い | 機能追加 | S | 不要 |
| **C-4** | 中 | 警告バッジが無い（クランプ到達・高レバ・欠損入力） | 機能追加 | M | 不要 |
| **C-5** | 低 | 順位の安定性が見えない | B-1が前提 | S | 不要 |
| **C-6** | 低 | 買収シナリオの注記が無い | 表示のみ | XS | 不要 |
| **C-7** | 低 | 営業利益・FCFが表示されない | 機能追加 | M | 不要 |

工数の目安：XS = 30分以内 / S = 半日 / M = 1〜2日

---

## 4. 各項目の詳細

---

### S-1（致命）粗利率の `floor` が「利益率の崩壊」を「4.29倍の改善」に反転させている

#### 現象

粗利率が崩壊している銘柄が、粗利率の**改善**として最大4.29倍の加点を受けている。

#### 根拠（実データ）

`(gm + total_change) < floor` となり `floor` が拘束している銘柄：

| 銘柄 | 順位 | 粗利率(当期) | 粗利率(前期) | 外挿の生値 | クランプ後 | margin_multiple |
|---|---|---|---|---|---|---|
| **AMR** | **6位** | 1.17% | 11.21% | **−13.83%** | **5.00%** | **4.29** |
| **KOS** | **18位** | 1.76% | 41.07% | **−13.24%** | **5.00%** | **2.84** |
| CLNE | 276位 | 3.84% | 20.10% | −11.16% | 5.00% | 1.30 |
| WNC | 388位 | 4.53% | 13.62% | −10.47% | 5.00% | 1.10 |

AMR の計算を追う：

```
annual_trend = (0.0117 − 0.1121) × 0.40 = −0.04016 /年
total_change = clamp(−0.04016 × 7 = −0.2811, −0.15, +0.15) = −0.15
current + total_change = 0.0117 − 0.15 = −0.1383
_clamp(−0.1383, floor=0.05, ceiling=0.90) = 0.05
margin_multiple = 0.05 / 0.0117 = 4.27
```

AMR の期待倍率3.76のほぼ全部がこの項である（売上倍率は0.70＝縮小見通し）。

#### 原因

`src/autoscreener/scoring/moic.py:344`

```python
return _clamp(current + total_change, margin.floor, margin.ceiling)
```

`floor` は「外挿が負の粗利率まで暴走しないための下限」として置かれているが、**現在の粗利率が `floor` を下回る銘柄では、下限が現在値を上から押し上げる加点装置になる**。

#### 修正案

外挿結果が現在値を下回る方向に出ているとき、フロアが現在値を**上回って**押し上げることを禁止する。

```python
def terminal_gross_margin(inputs: MoicInputs, config: ScoringConfig) -> float:
    margin = config.margin
    current = inputs.gross_margin_latest
    if inputs.gross_margin_prior is None:
        return _clamp(current, margin.floor, margin.ceiling)
    annual_trend = (current - inputs.gross_margin_prior) * margin.trend_damping
    total_change = _clamp(
        annual_trend * config.horizon_years, -margin.max_total_change, margin.max_total_change
    )
    # S-1: 現在の粗利率がフロアを下回っている銘柄では、フロアが「下限」ではなく
    # 「押し上げ」として働き、利益率の崩壊を改善に反転させてしまう。フロアは
    # 現在値より上へ持ち上げてはならない。
    lower_bound = min(current, margin.floor)
    return _clamp(current + total_change, lower_bound, margin.ceiling)
```

#### 実装手順

1. `src/autoscreener/scoring/moic.py:331-344` の `terminal_gross_margin` を上記に差し替える。
2. `tests/unit/test_moic.py` に回帰テストを追加（下記）。
3. `uv run pytest tests/unit/test_moic.py`
4. `uv run python -m autoscreener.cli run-backtest` → KPI が §2.2 と比べて悪化していないことを確認。
5. `uv run python -m autoscreener.cli run-scoring`
6. APIプロセスを再起動し、AMR / KOS の順位が下がったことを確認。

#### テスト

```python
def test_margin_floor_does_not_lift_a_collapsing_margin():
    """S-1: 粗利率が崩壊している銘柄で floor が改善に反転しないこと。

    AMR(2026-08-25、6位)の実データ:粗利率 11.21% → 1.17% の崩壊に対し、
    旧実装は floor=0.05 が下から押し上げて margin_multiple 4.29 を返していた。
    """
    config = _scoring_config()          # 既存のテストヘルパを使う
    inputs = _inputs(gross_margin_latest=0.0117, gross_margin_prior=0.1121)
    terminal = terminal_gross_margin(inputs, config)
    assert terminal <= inputs.gross_margin_latest
```

#### 受け入れ基準

- [ ] 上記テストが通る
- [ ] §2.3(c) のSQLが **0行** を返す
- [ ] `run-backtest` の `decile_monotonicity` / `rank_ic` / `lift_ratio` が §2.2 から悪化していない
- [ ] AMR・KOS の順位が下がっている

---

### S-2（高）粗利率の上限が絶対値(15pt)なので、薄利企業ほど機械的に有利

#### 現象

`margin.max_total_change: 0.15` は**絶対ポイント**での上限なので、同じ改善幅でも粗利率の水準によって倍率が桁違いに変わる。

| 現在の粗利率 | +7.8pt の改善 | margin_multiple |
|---|---|---|
| 3.8%（ALTO、4位） | → 11.6% | **3.06** |
| 50% | → 57.8% | 1.16 |

#### 根拠（実データ）

- `margin_multiple ≥ 1.5` は401銘柄中 **22件**、うち **12件が上位40位以内**（SENEA 2位・ALTO 4位・CLW 5位・AMR 6位・ACTG 7位・QNST 9位・CBZ 10位・MEI 15位・KOS 18位・LMRI 24位・LXU 26位…）
- 粗利率10%未満 かつ `mm ≥ 1.5` は **6件**（ALTO 4位・CLW 5位・AMR 6位・KOS 18位・BLDP）
- `+15pt` の上限に到達しているのは **25件**、うち5件が上位30位（VMET 1位・ACTG 7位が該当）

#### 原因

`config/scoring.yaml` の `margin.max_total_change` が絶対値のみで、相対上限が無い。

#### 修正案

絶対キャップを残したまま、**相対キャップ**を追加する。

`src/autoscreener/config.py` の `MarginConfig`：

```python
class MarginConfig(BaseModel):
    trend_damping: float = Field(ge=0, le=1)
    max_total_change: float = Field(ge=0)
    # S-2: 絶対ポイントの上限だけでは、粗利率が薄い銘柄ほど同じ改善幅が
    # 大きな倍率になる（3.8% → 11.6% は3.06倍、50% → 57.8% は1.16倍）。
    # 終端粗利率が現在の何倍までを許すかの相対上限。大きな値で実質無効。
    max_relative_change: float = Field(gt=1.0, default=100.0)
    floor: float = Field(gt=0, lt=1)
    ceiling: float = Field(gt=0, le=1)
```

`moic.py` の `terminal_gross_margin` 末尾（S-1の修正と統合）：

```python
    lower_bound = min(current, margin.floor)
    terminal = _clamp(current + total_change, lower_bound, margin.ceiling)
    # S-2: 相対上限。薄利企業の絶対ポイント改善が過大な倍率にならないようにする。
    return min(terminal, current * margin.max_relative_change)
```

`config/scoring.yaml`：

```yaml
margin:
  trend_damping: 0.40
  max_total_change: 0.15
  # S-2（2026-08-26追加）:粗利率が薄い銘柄ほど絶対ポイントの上限が過大な倍率に
  # なる問題への対処。1.5 は「7年で粗利率が1.5倍になるところまでは認める」という
  # 意味で、擬似バックテストで選んだ値（弱くしか特定されていない）。
  max_relative_change: 1.5
  floor: 0.05
  ceiling: 0.90
```

#### 実装手順

1. `MarginConfig` にフィールドを追加（**既定値を大きく取り、既存の設定ファイルを壊さないこと**）。
2. `moic.py` に相対キャップを追加。
3. `config/scoring.yaml` に `max_relative_change: 1.5` を追加。
4. **`max_relative_change` を 1.3 / 1.5 / 2.0 / 100.0（無効）の4通りで `run-backtest` を回し、KPIの変化表を作る。** 表は本節に追記する。
5. KPIが最良の値を採用。差が無ければ最も保守的（小さい）値を採る。
6. `run-scoring` → API再起動。

#### 注意

**これは「擬似バックテストで選んだ値」に分類される**（`config/scoring.yaml` 冒頭のコメント参照）。独立な観測期間が実質3つしかないため、細かく合わせ込むと過学習になる。**0.1刻みで最適化しない。**

#### 受け入れ基準

- [ ] `uv run pytest` が通る（設定スキーマの検証テスト含む）
- [ ] 4通りのKPI比較表が本節に記録されている
- [ ] `margin_multiple ≥ 2.0` の銘柄が上位30から消えるか、大幅に減る
- [ ] `decile_monotonicity` / `rank_ic` が §2.2 から悪化していない

---

### S-3（中）粗利率トレンドが「直近2期の差分1回」しかない

#### 現象

粗利率の7年外挿の根拠が、**直近2期の差分1つ**しかない。シクリカル銘柄の1年の変動をそのまま7年へ引き伸ばしている。

#### 根拠（実データ）

| 銘柄 | 順位 | 前期 | 当期 | 外挿先 | 実態 |
|---|---|---|---|---|---|
| ALTO | 4位 | 1.01% | 3.80% | 11.63% | エタノール。市況の1年 |
| SENEA | 2位 | 9.51% | 13.93% | 26.30% | 缶詰。26%は業界水準を大きく超える |
| CLW | 5位 | 5.50% | 7.43% | 12.84% | 紙。市況 |

#### 原因

`src/autoscreener/scoring/moic.py:331-344`。`MoicInputs` が粗利率を2期分しか持っていない。

#### 修正案（2案。どちらかを選ぶ）

**案A（軽い）**：`trend_damping` を 0.40 → 0.20 に下げる。実装コストほぼゼロ、KPIで検証。

**案B（本筋）**：粗利率の系列を3期以上持ち、最小二乗の傾きを使う。

1. `MoicInputs` に `gross_margin_history: tuple[float, ...] | None = None` を追加（**古い順**）。
2. `point_in_time.build_moic_inputs` で `Gross Profit` / `Total Revenue` の年次系列から粗利率系列を組み立てて渡す。
   - **重要**：`available_from` によるポイントインタイム制約を壊さないこと。過去時点で見えない期を混ぜるとバックテストが無意味になる（14.3）。
3. `terminal_gross_margin` で、系列が3点以上あれば最小二乗の傾きを `annual_trend` に使い、2点以下なら現行の差分にフォールバック。
4. `MoicInputs.to_dict` は `self.__dict__` を舐めるので永続化は自動。ただし **tuple はJSONで list になる**ため、`from_dict` で tuple に戻す処理が要る。

#### 実装上の注意

- `MoicInputs` にフィールドを追加すると `scores.inputs` のJSONスキーマが変わる。**過去に保存した行には新フィールドが無い**ので、既定値 `None` で必ずフォールバックすること（`from_dict` は既知フィールドのみ拾う実装なので後方互換は保たれる）。
- APIの任意ホライズン再計算（27.24）は `scores.inputs` から `MoicInputs` を復元する。古い日付のスコアが新フィールドを持たないのは正常。

#### 受け入れ基準

- [ ] 案A/案Bのどちらを採ったか本節に記録
- [ ] `tests/unit/test_point_in_time.py` に「3期以上あれば傾きを使う」テストを追加
- [ ] ポイントインタイム制約のテスト（既存）が引き続き通る
- [ ] `run-backtest` のKPIが悪化していない

---

### S-4（中）利益率の谷にある銘柄で「EV/粗利の据え置き」が逆向きに効く

#### 現象

28.2 は「割安だから上がる、という無償のリレーティング」を撤廃した。しかし**その鏡像（割高の据え置き）が残っている。**

#### 根拠（実データ）

AMR（6位）：

```
粗利率 1.17%（谷）→ 粗利 $24.8M
EV/粗利 = 93.9倍          ← 分母が谷だから高いだけ
モデルの想定：粗利が 0.70 × 4.29 = 3.0倍に回復
モデルの倍率：93.9 → 110.8（×1.18、成長減速で拡大方向）
結果：EV が 3.5倍
```

粗利が正常化すれば EV/粗利は当然縮む。**粗利の回復と倍率の据え置きを同時に主張しているので二重取りになっている。**

同型の問題：VMET（1位、EV/粗利 98.9倍）、BRUN（8位、49.7倍）。

#### 原因

`src/autoscreener/scoring/moic.py:640` 付近

```python
current_multiple = enterprise_value / inputs.gross_profit_latest
```

分母が「直近1期の粗利」なので、粗利が一時的に潰れている銘柄では `current_multiple` が構造的に膨らむ。`growth_fade_multiple_change` は成長率の差分でしか倍率を動かさないため、この膨らみが7年後まで温存される。

#### 修正案（3案。S-3案Bと相性が良い）

**案A（推奨）**：`current_multiple` の分母に**正規化粗利**（粗利率の3〜5期中央値 × 直近売上）を使う。

**案B**：EV/粗利ではなく EV/売上で倍率を測り、粗利率の変化は `margin_multiple` 側だけで表現する（二重計上を構造的に排除）。ただし κ の推定も EV/売上ベースで測り直す必要がある（`estimate-elasticity`）。影響範囲が大きい。

**案C（最小）**：`current_ev_to_gross_profit` が断面の上位パーセンタイル（例：95%点）を超える銘柄を Tier 2 に回す。モデルは変えず、適用範囲を狭めるだけ。即日入る対症療法。

#### 実装手順（案A）

1. S-3案Bで `gross_margin_history` を持たせる。
2. `moic.py`：

```python
   # S-4: 粗利率が一時的に潰れている銘柄では EV/粗利 の分母が谷になり、
   # 倍率が構造的に膨らむ。その膨らみを7年後まで温存すると、粗利の回復と
   # 倍率の据え置きを同時に主張することになり二重取りになる。
   normalized_margin = (
       statistics.median(inputs.gross_margin_history)
       if inputs.gross_margin_history and len(inputs.gross_margin_history) >= 3
       else inputs.gross_margin_latest
   )
   current_multiple = enterprise_value / (inputs.revenue_latest * normalized_margin)
```

3. **恒等式の閉包テスト**（27.21 の「5因子の積が `expected_moic` と厳密に一致する」）が壊れないか確認する。`tests/unit/test_moic.py:423` 付近。分母を変えると `margin_multiple` の定義とも整合を取る必要がある。**ここは慎重に。**
4. `run-backtest` でKPI確認。

#### 受け入れ基準

- [ ] 採用した案を本節に記録
- [ ] 恒等式の閉包テストが通る（`expected_moic == 5因子の積`）
- [ ] AMR / VMET の順位が下がる、または Tier 2 に移る
- [ ] `run-backtest` のKPIが悪化していない

---

### S-5（高）`net_debt` にオペレーティングリース債務が入り、小売・外食が上位に来る

#### 現象

リース債務を金融負債として扱っているため、店舗網を持つ企業の `leverage_effect` が過大になり、**本業が縮小していても上位に来る。**

#### 根拠（実データ）

| 銘柄 | 順位 | 時価総額 | ネットデット | ND/時価総額 | leverage_effect | 本業の成長(決算) |
|---|---|---|---|---|---|---|
| **DBI**（靴小売） | **3位** | $280M | **$1,156M** | 4.12x | **2.14** | **−4.4%** |
| SMC | 16位 | $470M | $1,036M | 2.20x | 2.07 | — |
| KOS | 18位 | $1,703M | $2,961M | 1.74x | 2.10 | — |
| CODI | 28位 | $867M | $1,809M | 2.09x | 1.76 | — |
| AFYA | 19位 | $1,258M | $1,995M | 1.59x | 1.50 | — |
| BJRI（外食） | 13位 | $1,431M | $467M | 0.33x | 1.16 | — |

上位30の**17%** が `leverage_effect ≥ 1.5`（31位以下では **0%**）。

#### 原因（2つある）

**(1) リース債務の混入** — `src/autoscreener/scoring/point_in_time.py:333-335`

```python
total_debt = _latest(balance_sheet, "Total Debt") or 0.0
...
net_debt = total_debt - cash
```

yfinance の `Total Debt` は `Long Term Debt And Capital Lease Obligation` + `Current Debt And Capital Lease Obligation` を含む。ASC 842 以降、店舗リースは全部ここに乗る。

**(2) ネットデットが7年間名目一定** — `src/autoscreener/scoring/moic.py:648-655`

```python
terminal_equity = terminal_ev - inputs.net_debt
```

金利支払いもリース更新もFCFによる返済も無い。EV上昇分は全額株主に行く。**レバレッジの上振れだけを取り、コストを取っていない。**

#### 修正案

**段階1（即日、挙動を変えない）**：診断値として分離して可視化する。

1. `MoicInputs` に `lease_liability: float | None = None` を追加。
2. `point_in_time.py` で `Capital Lease Obligations` 系のキーから抽出する。**yfinanceの実データでどのキーが取れるかを先に調べること**（下記の調査SQL）。
3. `engine.result_to_factors` に `lease_share_of_net_debt` を追加。
4. C-4 の警告バッジで「レバレッジ 2.1倍・うちリース 80%」を表示。

**段階2（挙動を変える）**：以下のいずれか。KPIで選ぶ。→ §8-1 でオーナー確認。

- **2a**：リース債務を `net_debt` から除外する（＝EVから外す）。ただし粗利にリース費用が入っていない企業では過小評価になる。
- **2b**：ネットデットを7年間で一定率ずつ償却する（例：FCFの一定割合で返済）。
- **2c**：`leverage_effect` に上限を設ける（例：2.0）。最も安直だが即効性がある。

#### 調査SQL（実装前に必ず実行）

```sql
SELECT k, v
FROM raw_snapshots r
JOIN tickers t ON t.id = r.ticker_id,
LATERAL jsonb_each(r.payload->'balance_sheet') AS e(k, v)
WHERE t.symbol = 'DBI'
  AND r.snapshot_date = (SELECT max(snapshot_date) FROM raw_snapshots)
  AND (k ILIKE '%debt%' OR k ILIKE '%lease%');
```

#### 受け入れ基準

- [ ] 段階1：`lease_share_of_net_debt` が `scores.factors` に入り、UIに出る
- [ ] 段階2：採用案と、2a/2b/2c それぞれのKPI比較表が本節に記録されている
- [ ] DBI の順位が実態（本業 −4.4%）と整合する水準まで下がる
- [ ] `run-backtest` のKPIが悪化していない

---

### S-6（中）成長率の上限60%への張り付きが上位に集中し、順位が決まらない

#### 現象

`growth.max_initial_rate: 0.60` に張り付いた銘柄は成長率が**全員同じ 0.600** になり、順位は他因子のノイズで決まる。しかも「上限に当たった＝この数字は信用できない」というシグナルが、満点の加点として扱われている。

#### 根拠（実データ）

| 帯 | 上限60%張り付き |
|---|---|
| **上位30** | **17%**（5銘柄） |
| 31〜100位 | 1% |
| 101位以下 | 1% |

| 銘柄 | 順位 | 3年CAGR | 直近YoY | 採用値 | 売上 |
|---|---|---|---|---|---|
| VMET | 1位 | **+232.5%** | +189.1% | 0.600 | **$35M** |
| ACTG | 7位 | +68.9% | +133.2% | 0.600 | $285M |
| BRUN | 8位 | (欠損) | +238.8% | 0.600 | **$27M** |

#### 原因

`src/autoscreener/scoring/moic.py:232`

```python
return _clamp(min(candidates), growth.min_initial_rate, growth.max_initial_rate)
```

`_clamp` は「当たったこと」を呼び出し元に伝えない。

#### 修正案

**段階1（表示のみ、即日）**：クランプ到達をフラグとして持ち回る。

1. `MoicResult` に `growth_rate_clamped: bool` を追加。
2. `compute_moic` 内で `min(candidates) > growth.max_initial_rate` を判定する（`base_initial_growth` の戻り値型を変えるより変更が小さい）。
3. `engine.result_to_factors` に `growth_rate_clamped` を **float（0.0/1.0）** で追加。`factors` は `dict[str, float]` のため。
4. C-4 の警告バッジで表示。

**段階2（挙動を変える）**：→ §8-3 でオーナー確認。

- 上限到達銘柄の σ を広げる（推定の不確かさを確率に反映する、最も筋が良い）
- 上限到達銘柄を Tier 2（監視リスト）へ回す
- `max_initial_rate` を下げる（0.40 等）— **他の銘柄にも影響するので慎重に**

#### 受け入れ基準

- [ ] `scores.factors.growth_rate_clamped` が入る
- [ ] UIで「成長率が上限に張り付いています」が出る
- [ ] 段階2を実施した場合、KPI比較表が記録されている

---

### S-7（中）`min(CAGR, YoY)` の安全装置が、片方欠損だと無音で無効化する

#### 現象

買収連結による見かけの成長（IMMR型、27.13）を防ぐための「食い違ったら遅いほうを信じる」という仕組みが、**成長率の観測が1つしかない銘柄では発動しない。**

#### 根拠（実データ）

BRUN（8位）：`revenue_cagr = NULL`、`revenue_yoy = 2.388`。売上$27M、上場直後。検証なしで上限60%を受け取っている。

該当率：上位30の **3%**、それ以外 1%。

#### 原因

`src/autoscreener/scoring/moic.py:226-232`

```python
candidates = [g for g in (inputs.revenue_cagr, inputs.revenue_yoy) if g is not None]
if not candidates:
    return None
return _clamp(min(candidates), growth.min_initial_rate, growth.max_initial_rate)
```

`candidates` が1要素でも `min()` は成立してしまう。

#### 修正案

観測が1つしかない銘柄には、より保守的な上限を適用する。

`config.py` の `GrowthConfig`：

```python
    # S-7: 成長率の観測が1つしかない銘柄（3年CAGRか直近YoYの片方が欠損）は、
    # 27.13 の「食い違ったら遅いほうを信じる」という安全装置が働かない。
    # そうした銘柄に適用する、より保守的な初期成長率の上限。
    max_initial_rate_single_observation: float = Field(default=0.30)
```

`moic.py`：

```python
def base_initial_growth(inputs: MoicInputs, config: ScoringConfig) -> float | None:
    growth = config.growth
    candidates = [g for g in (inputs.revenue_cagr, inputs.revenue_yoy) if g is not None]
    if not candidates:
        return None
    ceiling = (
        growth.max_initial_rate
        if len(candidates) >= 2
        else min(growth.max_initial_rate, growth.max_initial_rate_single_observation)
    )
    return _clamp(min(candidates), growth.min_initial_rate, ceiling)
```

#### 受け入れ基準

- [ ] `tests/unit/test_moic.py` に「観測が1つのときは保守的な上限が使われる」テストを追加
- [ ] BRUN の順位が下がる
- [ ] `run-backtest` のKPIが悪化していない

---

### S-8（中）価格ナウキャストが上限に張り付き、実質モメンタム加点になっている

#### 現象

28.3 は「**これはモメンタム戦略ではない**」と明記しているが、実測では上位の3割が ±15pt の上限に張り付いており、**決算上は縮小している企業を成長企業に読み替えている。**

#### 根拠（実データ）

| 帯 | ±15pt 上限張り付き |
|---|---|
| 上位30 | 23% |
| **31〜100位** | **30%** |
| 101位以下 | 21% |

符号を反転させている例：

| 銘柄 | 順位 | 決算ベース g | 補正後 g | 補正量 |
|---|---|---|---|---|
| ALTO | 4位 | **−11.7%** | +3.3% | +15.0pt（上限） |
| MEI | 15位 | **−4.8%** | +10.2% | +15.0pt（上限） |
| BJRI | 13位 | +2.9% | +17.9% | +15.0pt（上限） |
| EFXT | 14位 | +6.5% | +21.5% | +15.0pt（上限） |
| DBI | 3位 | **−4.4%** | +6.6% | +11.0pt |
| KOP | 12位 | **−10.2%** | −0.1% | +10.1pt |

#### 原因

`src/autoscreener/scoring/moic.py:274`

```python
adjustment = _clamp(
    growth.nowcast_weight * excess / elasticity, -growth.nowcast_cap, growth.nowcast_cap
)
```

`nowcast_weight: 0.25` / `nowcast_cap: 0.15` / `elasticity: 0.86` なので、超過対数リターンが **+0.516（＝市場対比 +67%）** を超えると上限に張り付く。1年で市場を67%上回る銘柄は小型株では珍しくないため、上限が常時拘束される。

#### 修正案

**必ず(1)を実施し、(2)(3)はKPIで選ぶ。**

1. **上限張り付き率を監視KPIにする。** `backtest/metrics.py` に `nowcast_cap_hit_rate` を追加し、`/validation` に表示。**設計意図と実挙動の乖離を継続的に見るため。**
2. `nowcast_cap` を 0.15 → 0.10 に下げる。
3. **符号を反転させる補正に別の上限を置く。** 決算が縮小を示している銘柄を成長企業に変える補正は、一次情報を株価で上書きする行為なので、より強い証拠を要求する。

```python
    raw_adjustment = growth.nowcast_weight * excess / elasticity
    cap = growth.nowcast_cap
    # S-8: 決算が縮小を示している銘柄を「成長している」に反転させる補正は、
    # 一次情報（決算）を株価で上書きする行為であり、通常の補正より強い証拠を
    # 要求する。反転方向の補正には別の（より狭い）上限を適用する。
    if base_growth < 0 and raw_adjustment > 0:
        cap = min(cap, growth.nowcast_cap_sign_flip)
    adjustment = _clamp(raw_adjustment, -cap, cap)
```

`GrowthConfig` に `nowcast_cap_sign_flip: float = Field(ge=0, default=0.05)` を追加。

#### 受け入れ基準

- [ ] `nowcast_cap_hit_rate` が `/validation` に出る
- [ ] 3通り以上（現状 / cap=0.10 / 反転上限あり）のKPI比較表が記録されている
- [ ] 上限張り付き率が31〜100位で30% → 15%以下になる
- [ ] `run-backtest` のKPIが悪化していない

---

### S-9（中）σ の85%縮小により、リスク項が順位から消えている

#### 現象

`uncertainty.sigma_shrinkage: 0.85` により σ の銘柄差が15%しか残らず、順位は `expected_moic` とほぼ一致する（順位相関 **+0.982**）。結果として **生存確率の順位相関は +0.031**、つまり順位はリスク調整されていない。

#### 根拠（実データ）

KOS（18位）は生存確率 **33%**。7年で3分の2が消える見通しの銘柄が上位20位に入っている。

#### 論点

これは**バグではなく設計判断**であり、28.4 に「現在のデータで σ の銘柄差を主張できるだけの根拠が無いという状態表明である」と明記されている。しかし：

- 商品名は「P(7年で10倍)」であり、リスク調整されている**ように見える**
- README にはこの説明があるが、**ランキング画面には無い**

#### 修正案（3案。実装は独立に可能）

**案1（表示のみ、推奨・即日）**：C-4 と統合。
- `survival_probability < 0.50` に警告バッジ
- ランキング画面上部に「順位は実質的にリスク未調整の期待倍率の順序です」を明記

**案2（挙動を変える）**：→ §8-2 でオーナー確認。

```python
class RequirementsConfig(BaseModel):
    ...
    # S-9: 生存確率がこれを下回る銘柄はランキングしない。σ の縮小により
    # リスクが順位にほとんど反映されないため、下限で構造的に排除する。
    # 0.0 で無効（既定）。
    min_survival_probability: float = Field(ge=0.0, lt=1.0, default=0.0)
```

**案3**：`sigma_shrinkage` を下げる。**非推奨** — 28.4 の実測では縮小を強めるほどKPIが改善しており、根拠に反する。

#### 受け入れ基準

- [ ] 案1は必ず実施
- [ ] 案2を実施する場合、`min_survival_probability` を 0.0 / 0.3 / 0.5 で回したKPI比較表を記録
- [ ] 生存確率30%台の銘柄が、上位に出るなら警告付きで出る

---

### A-1（中）希薄化の欠損を「希薄化ゼロ」として扱っている

#### 現象

`dilution_cagr` が欠損している銘柄は、**増資が一切ない**という最良のシナリオを無償で受け取る。15.1④ は希薄化を「単独で最大の改善余地」と呼んでいる軸である。

#### 根拠（実データ）

欠損率：上位30の **7%**、31〜100位 1%、101位以下 0%。BRUN（8位）が該当。

#### 原因

`src/autoscreener/scoring/moic.py:659-663`

```python
dilution_rate = _clamp(
    inputs.dilution_cagr if inputs.dilution_cagr is not None else 0.0,
    dilution.min_annual_rate,
    dilution.max_annual_rate,
)
```

27.1 の「欠損を減点に読み替えない」方針は正しいが、これは**欠損を満点に読み替えている**。

#### 修正案

断面の中央値を使う（減点でも満点でもない中立）。

1. `CrossSection` に `median_dilution_cagr: float | None = None` を追加。
2. `build_cross_section` の1周目で `inputs_list` から中央値を計算。
3. `compute_moic` で欠損時に中央値を使い、無ければ 0.0 にフォールバック。
4. **`CrossSection.to_dict` / `from_dict` は手書きなので、両方に新フィールドを追加すること**（`scores.inputs` に保存され、APIの任意ホライズン再計算で復元される）。

代替案：欠損銘柄を Tier 2 に回す。より保守的。

#### 受け入れ基準

- [ ] `CrossSection` の往復（`to_dict` → `from_dict`）テストが通る
- [ ] BRUN の希薄化が中立値になる
- [ ] `run-backtest` のKPIが悪化していない

---

### A-2（低）売上$27〜35Mの企業が EV/粗利 50〜99倍で上位を占める

#### 現象

恒等式は現在のEV/粗利をほぼ据え置くため、**すでに極端に高い倍率が7年後も維持される前提**になる。

| 銘柄 | 順位 | 売上 | 粗利 | EV/粗利 | 時価総額 |
|---|---|---|---|---|---|
| VMET | 1位 | $35M | $13.5M | **98.9x** | $1,166M |
| BRUN | 8位 | $27M | $23.0M | **49.7x** | $1,106M |
| AMR | 6位 | $2,129M | $24.8M | **93.9x** | $2,734M |

`requirements.min_gross_profit_usd: 1_000_000` は緩すぎる。

#### 修正案

1. `CrossSection` に `ev_to_gross_profit_p95: float | None` を追加（`build_cross_section` の1周目で計算）。
2. `RequirementsConfig` に `max_ev_to_gross_profit_percentile: float = Field(default=1.0)` を追加。
3. 超過銘柄は `compute_moic` が `None` を返す（＝Tier 2 行き）。**理由を区別できるよう `unranked_reason` に `extreme_valuation` を持たせる**（27.20 の「測れなかった」と「測った結果」を分ける方針に従う）。

#### 注意

S-4（正規化粗利）を実施すると AMR のような「分母が谷」ケースは自動的に解消する。**S-4 を先に済ませてから、それでも残る銘柄に対して A-2 を適用するか判断すること。** → §8-4

#### 受け入れ基準

- [ ] S-4 実施後の EV/粗利 分布を確認したうえで、A-2 が必要かを判断し本節に記録
- [ ] 実施する場合、`unranked_reason` が `/watchlist` で正しく分類される

---

### B-1（高）v4のスコアが1日分しか存在しない

#### 現象

```
score_date  | scoring_version | count
2026-08-25  | v4              | 657     ← v4 はこの1日だけ
2026-08-25  | v3              | 649
2026-08-24  | v3              | 632
```

- `/rank-changes`（直近2日比較）は **v4 では機能しない**
- 銘柄詳細の確率推移が1点のみ
- **順位が安定なのか毎日入れ替わるのかが観測不能**

#### 修正

`universe_snapshots`（ゲート判定結果）は **2026-08-23 / 08-24 / 08-25 の3日分**あるため、v4スコアを過去2日分バックフィルできる。

```bash
uv run python -m autoscreener.cli run-scoring --date 2026-08-23
uv run python -m autoscreener.cli run-scoring --date 2026-08-24
```

#### 注意

- `run_scoring` は該当日の `universe_snapshots` が無いと何もせず終了する（`engine.py:311`）。上記3日以外はバックフィルできない。
- `price_snapshots` は **2023-08-22 〜 2026-08-25 の755営業日分**あるので、ナウキャストに必要な12ヶ月リターンは過去日でも算出できる。
- **モデルを修正するたびに、この3日分を再実行して順位の変化を比較すること。** 各修正の受け入れ確認に使える。

#### 受け入れ基準

- [ ] `SELECT score_date, count(*) FROM scores WHERE scoring_version='v4' GROUP BY 1;` が3行返る
- [ ] `/rank-changes` が v4 で動く
- [ ] 銘柄詳細の確率推移が3点になる

---

### B-2（低）検証は1年ホライズン、商品は7年

`backtest_runs.horizon_years = 0.9993`。7年の実測は存在しない。28.16 に記載済みだが、**ランキング画面には出ていない**。

**修正**：`frontend/src/pages/RankingPage.tsx` のヘッダに「このモデルは1年ホライズンでしか検証されていません（rank IC +0.148）」を追加。`/validation` へのリンクを併記。

---

### B-3（低）較正済み確率の有効数字が実態を超えている

上位30の「1年オンペース率」は全部31%台（31.9 / 31.7 / 31.7 / 31.5 / 31.4 …）。較正が観測範囲外へ外挿しないための飽和であり、**銘柄間の差は存在しない**。

**修正**：`RankingPage.tsx:249-252`

```tsx
{item.calibrated_on_pace_probability != null
  ? `${(item.calibrated_on_pace_probability * 100).toFixed(1)}%`
  : "—"}
```

較正曲線の最上位ビンに入っている銘柄は `31%（上限帯）` のように表示する。判定には `/api/v1/backtest/latest` の較正ビン情報が使える。最小実装としては「上位N件は同一値」と注記するだけでもよい。

---

### B-4（低）最悪日リフト0.895がランキング画面に出ていない

8評価日中1日は上位デシルがユニバースに負けている。README と `/validation` にはあるが、ランキング画面本体に無い。

**修正**：`RankingPage.tsx` の冒頭注意書きに1行追加。

---

### B-5（中）`delisted_at` が5,312銘柄中0件

#### 現象

```sql
SELECT count(*) FILTER (WHERE delisted_at IS NOT NULL) FROM tickers;  -- 0
```

一方で日次ログには 404 が出続けている：

```
2026-08-26 09:01:30 ERROR yfinance: HTTP Error 404: Quote not found for symbol: AHL$D
2026-08-26 09:05:31 ERROR yfinance: HTTP Error 404: Quote not found for symbol: BAC$L
```

`errors.py:78` は 404 を `PermanentFailure` に分類し、`snapshot_collector.py:185` は `delisted_at` を設定する実装になっている。**にもかかわらず1件も設定されていない。**

#### 疑い（要調査）

yfinance の `.info` は内部でHTTPエラーを握りつぶし、例外を送出せず空/部分的な dict を返す可能性がある。その場合 `EmptyResponseError` として扱われ、`delisted_at` は設定されない。`/universe/status` の `empty_response: 6` という少なさとも整合しないので、**実際にどの経路を通っているかをログか再現テストで確認すること。**

#### なぜ重要か

`delisted_at` が埋まらないと、**バックテストの生存バイアス（`delisted_settlement_rate = 0.00%`）が将来も解消しない。** 28.16 は「データ源の制約であり解消不能」としているが、それは**過去**の話であり、**今日以降に上場廃止される銘柄を記録できるかは実装の問題**である。ここが動かないと、1年後も2年後もバックテストは生存バイアスを抱えたままになる。

#### 調査手順

```bash
uv run python -m autoscreener.cli collect --symbols 'AHL$D'
```
```sql
SELECT * FROM collection_logs ORDER BY id DESC LIMIT 20;
```

#### 受け入れ基準

- [ ] 404 が実際にどの `CollectionError` に分類されているかが判明し、本節に記録されている
- [ ] 恒久的に取得できないシンボルで `tickers.delisted_at` が設定される
- [ ] `tests/unit/test_yfinance_errors.py` に回帰テストが追加されている

---

### B-6（低）`/universe/status` が実行途中の数字をそのまま返す

監査時点（2026-08-26 昼）の応答：

```json
"collection_status_counts": {"success": 1215, "invalid_data": 288, "empty_response": 6}
```

これは9:00開始の日次収集が5,312銘柄中1,509件まで進んだ時点の途中経過。**完了しているかどうかの表示が無い**ため「1,215銘柄しか取れていない」と誤読される。

**修正**：`api/routes.py:511` の `universe_status` に、当日の収集が完了しているかのフラグ（例：`collection_complete: bool`、`collection_progress: "1509/5312"`）を追加する。判定は `raw_snapshots` の当日件数と収集対象件数の比較でよい。

---

### B-7（低）"invalid_data" ラベルが除外を意味するように見える

`raw_snapshots.is_valid` はスコアリングを止めない（`grep -rn "is_valid" src/autoscreener` で確認済み。使用箇所は記録のみ）。しかし UI の "invalid_data" は除外されたように読める。

2026-08-25 の内訳（986/5,284 = 18.7%）：

| エラー | 件数 |
|---|---|
| `grossMargins_zero_suspected_missing` | 429 |
| `operatingMargins_out_of_range` | 362 |
| `currency_mismatch` | 262 |
| `market_cap_price_shares_mismatch` | 76 |
| `negative_revenue` | 19 |
| `grossMargins_out_of_range` | 8 |

**修正**：ラベルを「一部フィールドを無効化して採用」等に変え、`sanitize_info` が実際に何を落としたかを併記する。

---

### B-8（低）ユニバースから消えた銘柄が `tickers` に残り続ける

#### 現象

`tickers` に `$` を含むシンボルが **24件** 残っている（`AHL$D`, `BAC$L`, `DBRG$H`, `EPR$E`, `GAB$H` …）。

#### 原因

`universe_source.filter_candidates` には **`$` を除外するフィルタが既に実装済み**（`_NON_COMMON_SYMBOL_MARKERS`）。つまりこれらはフィルタ実装**前**に登録された残骸である。

`src/autoscreener/batch/universe_refresh.py:33-56` の `refresh_universe` は **挿入専用**で、候補リストから消えた銘柄を非活性化しない。

```python
for candidate in candidates:
    ticker = get_or_create_ticker(session, candidate.symbol, market="US")
    ...
```

`run_daily_collection.select_collectable_symbols` は `tickers` から `is_quarantined == False` の全件を取るため、消えた銘柄も毎日叩き続ける。

#### 修正案

`refresh_universe` に「今回の候補リストに存在しない `tickers` を非活性化する」処理を追加する。

- 既存の `is_quarantined` を流用するか、`delisted_at` を使うか（**B-5 と設計を揃えること**）
- **削除はしない。** `forward_returns` や `backtest_runs` が参照している可能性がある。フラグで落とす。
- **週次のユニバース再取得でしか走らない**点に注意（README「日次自動実行について」参照）。

#### 受け入れ基準

- [ ] `$` を含む24銘柄が収集対象から外れる
- [ ] 日次ログから 404 が消える
- [ ] `tests/unit/test_universe_source.py` に「候補から消えた銘柄が非活性化される」テストを追加

---

### C-1（中）下振れ確率が画面に無い

`scores.log_moic_mu` と `log_moic_sigma` が保存済みなので、**計算式1行**で出せる。

```python
# P(MOIC < x) = Φ((ln x − mu) / sigma)。上場廃止(生存確率の裏)も損失として合成する
below = NormalDist().cdf((math.log(x) - mu) / sigma)
loss_probability = (1.0 - survival) + survival * below
```

**推奨する表示**：`P(半値以下)`（x=0.5）と `P(元本割れ)`（x=1.0）の2つ。ランキング表と銘柄詳細の両方に。

#### 実装手順

1. `api/schemas.py` の `CandidateListItem` / `CandidateDetail` に `probability_below_half` / `probability_below_one` を追加
2. `api/routes.py` の候補一覧・詳細で計算（ホライズン再計算のロジックと同じ場所）
3. `frontend/src/api/types.ts` の `CandidateSummary`（15-36行）と `CandidateDetail`（74-100行）に追加
4. `RankingPage.tsx` に列を追加、`frontend/src/glossary.ts` に用語を追加して `<Term>` でリンク

---

### C-2（低）EV/粗利の自社ヒストリーが無い

「今のバリュエーションが自社の過去レンジのどこか」が分からない。`price_snapshots`（2023-08-22〜、755営業日）と年次決算から算出可能。S-3案B / S-4案A で粗利率系列を持てば実装しやすい。

---

### C-3（低）建てられるサイズ（流動性の実額）が無い

`universe.yaml` の `min_daily_dollar_volume_usd: 1_000_000` は一律の合否判定のみ。**実額と「日次売買代金の1%で建てるなら何日かかるか」**を銘柄詳細に出す。`price_snapshots.volume × close` から算出。

**関連**：`universe.yaml` のコメントに「⚠ 暫定値：単日出来高の代理指標。`price_snapshots` 蓄積後に必ず再calibrationすること」とある。755営業日たまっているので今すぐ実施できる。→ §8-5

---

### C-4（中）警告バッジが無い

S-5 / S-6 / S-7 / S-8 / S-9 / A-1 の**表示側の受け皿**。これを先に作ると、モデル修正を待たずに利用者へリスクを伝えられる。

| バッジ | 条件 | 由来 |
|---|---|---|
| 高レバレッジ | `leverage_effect >= 1.5` | S-5 |
| リース比率高 | `lease_share_of_net_debt >= 0.5` | S-5 |
| 成長率が上限 | `growth_rate_clamped == 1.0` | S-6 |
| 成長率の観測が1つ | `revenue_cagr` か `revenue_yoy` が欠損 | S-7 |
| 株価で成長を上方修正 | `growth_nowcast_adjustment >= 0.10` | S-8 |
| 生存確率が低い | `survival_probability < 0.50` | S-9 |
| 希薄化データなし | `dilution_cagr` 欠損 | A-1 |
| 粗利率の外挿が大きい | `margin_multiple >= 1.5` | S-2 |

#### 実装

- `factors` には既に大半の値が入っているので、**銘柄詳細ページだけなら フロント側だけで作れる**（`CandidateDetail.factors`）。
- ランキング一覧にも出すなら `CandidateListItem` に必要な因子を追加する必要がある（現在は `expected_moic` / `median_moic` / `survival_probability` のみ）。
- 既存の `Term` コンポーネント（`frontend/src/components/Term.tsx`）と `glossary.ts` の仕組みに乗せ、バッジにも用語解説を付ける。

---

### C-5（低）順位の安定性が見えない

B-1 のバックフィル後、銘柄詳細に「過去N日の順位レンジ」を出す。`scores` に `score_date` ごとの行があるので、`ROW_NUMBER()` で日ごとの順位を出して比較する。

---

### C-6（低）買収シナリオの注記が無い

小型の割安株ほど買収されやすく、10倍到達前に現金決済で終わる。モデルにこのシナリオは無い。ランキング画面の注意書きに1行追加する（実装コストはほぼゼロ、伝える価値は高い）。

---

### C-7（低）営業利益・FCFが表示されない

モデルは粗利までしか見ていない（恒等式の構造上）。ただし `health_index` の入力として `fcf_margin` は既に取得済み（`point_in_time.py:270`）。**表示するだけなら追加取得は不要。** 銘柄詳細に「FCFマージン」を診断値として出す。

---

## 5. 実装順序

### フェーズ1：基準線をつくる（半日）

1. **B-1** v4スコアを 08-23 / 08-24 にバックフィル
2. §2.2 のKPI基準線と §2.3 のSQL結果をスナップショットとして保存

> **理由**：以降のすべての修正は「順位がどう変わったか」で評価する。比較対象が無い状態で挙動を変えてはいけない。

### フェーズ2：明確な論理破綻を直す（半日）

3. **S-1** 粗利率フロアの押し上げ（1行、`run-backtest` 必要）

> **理由**：唯一「議論の余地なく間違っている」項目。AMR 6位・KOS 18位に直接効く。

### フェーズ3：ロジックを変えずにリスクを可視化する（1〜2日）

4. **S-6段階1** クランプ到達フラグを `factors` に追加
5. **C-1** 下振れ確率（`mu`/`sigma` から計算するだけ）
6. **C-4** 警告バッジ（上記＋既存の因子で作れるもの）
7. **B-2 / B-3 / B-4 / C-6** 表示の注記

> **理由**：モデルを触らずに、利用者が誤解している点をすべて画面に出せる。**上位30の半数以上が何らかのクランプに当たっている**という事実を、モデル修正を待たずに伝えられる。

### フェーズ4：レバレッジの扱いを直す（1〜2日）

8. **S-5段階1** リース債務の分離と可視化
9. **S-5段階2** 挙動変更（KPIで案を選ぶ）

> **理由**：3位・16位・18位・28位の主因。段階1だけでも十分に価値がある。

### フェーズ5：粗利率モデルの改訂（2〜3日、KPI検証を伴う）

10. **S-3** 粗利率系列を持つ（案A→案Bの順で試す）
11. **S-2** 相対キャップ
12. **S-4** 正規化粗利による EV/粗利
13. **A-2** S-4後に必要か判断

> **理由**：互いに依存している。S-3で系列を持てばS-4が容易になる。**まとめて設計してから着手すること。**

### フェーズ6：残り（1〜2日）

14. **S-7** 単一観測の上限
15. **S-8** ナウキャストの上限（監視KPIは先に入れる）
16. **A-1** 希薄化欠損の中立化
17. **S-9** 生存確率の下限（判断が必要 → §8）
18. **B-5 / B-8** データ品質
19. **B-6 / B-7 / C-2 / C-3 / C-5 / C-7**

---

## 6. 共通の作業手順

### 6.1 モデルを変更したときの必須手順

```bash
# 1. テスト
uv run pytest

# 2. バックテスト（KPIの変化を §2.2 と比較する）
uv run python -m autoscreener.cli run-backtest

# 3. スコアリング（3日分すべて）
uv run python -m autoscreener.cli run-scoring --date 2026-08-23
uv run python -m autoscreener.cli run-scoring --date 2026-08-24
uv run python -m autoscreener.cli run-scoring

# 4. APIプロセスを再起動（--reload なしの場合は必須）

# 5. §2.3 のSQLを再実行し、クランプ到達率が下がったことを確認
```

**順序を守ること。** `config/scoring.yaml` を変えると `config_hash` が変わり較正写像が無効化されるため、`run-backtest` を先に回さないと「1年オンペース率」が全銘柄 `—` になる。

### 6.2 `MoicInputs` / `CrossSection` にフィールドを追加するとき

- `MoicInputs.to_dict` は `self.__dict__` を舐めるので**追加は自動的に永続化される**
- `MoicInputs.from_dict` は既知フィールドのみ拾うので**後方互換は保たれる**（古い行は既定値になる）
- **tuple / list を持たせる場合は `from_dict` で型を戻す処理が要る**（JSONは list になる）
- `CrossSection` は `to_dict` / `from_dict` が**手書き**なので、追加時は両方に書くこと

### 6.3 `MoicResult` に診断値を追加するとき

1. `moic.py` の `MoicResult` にフィールド追加
2. `compute_moic` の `return MoicResult(...)` に追加
3. `engine.py:97` の `result_to_factors` に追加（**`dict[str, float]` なので bool は 0.0/1.0 で入れる**）
4. `tests/unit/test_score_columns.py` の `MoicResult` 生成ヘルパを更新（コンストラクタ引数が増えるため既存テストが落ちる）
5. 必要なら `api/schemas.py` / `frontend/src/api/types.ts` に追加

### 6.4 API・フロントの変更

- APIのスキーマを変えたら **必ずプロセス再起動**
- 同一ポートを複数プロセスが LISTEN していると応答が不定になる（Windows）。`netstat -ano | findstr :8000` で確認し、**リローダー親とワーカー子の2つ**を止める
- フロントは `cd frontend && npm run build` で型チェック

---

## 7. 既知の地雷

| 地雷 | 対処 |
|---|---|
| `config/scoring.yaml` 変更後にAPIを再起動し忘れる | 「N validation errors for ScoringConfig」が出る。**設定ファイルではなくプロセスが古い**。設定を書き戻さないこと |
| `run-backtest` を回さずに `run-scoring` | 「1年オンペース率」が全銘柄 `—` になる |
| 恒等式の閉包テスト | `expected_moic == revenue_multiple × margin_multiple × multiple_change × leverage_effect ÷ dilution_drag` をテストで固定してある（27.21）。分母・分子の定義を変えるとここが落ちる。**UIの内訳表示が実際の計算と乖離しないための防波堤なので、テストのほうを緩めてはいけない** |
| ポイントインタイム制約 | `point_in_time.py` は「その時点で開示済みだったデータだけ」を組み立てる。開示ラグ90日を無視した入力を足すと、バックテストが未来情報を使って無意味になる（14.3） |
| `run-scoring --date` の前提 | 該当日の `universe_snapshots` が必要。現在あるのは 2026-08-23 / 08-24 / 08-25 のみ |
| `scores` の `scoring_version` 混在 | 同じ `score_date` に v3 と v4 が共存する。SQLでは必ず `scoring_version` で絞ること（絞らないと同一銘柄が二重に出る） |
| PowerShell で `&&` が使えない | Windows PowerShell 5.1。`A; if ($?) { B }` を使うか Bash 側で実行する |

---

## 8. 人間の判断が必要な未決事項

以下は技術的な最適解が一意に決まらない。**実装者が勝手に決めず、オーナーに確認すること。**

1. **S-5段階2の方針** — リース債務を EV から外す（2a）／ネットデットを償却する（2b）／`leverage_effect` に上限（2c）。会計上の解釈が絡むため、KPIだけで決めるべきではない。
2. **S-9の案2を採るか** — 生存確率の下限を設けるとランキング件数が減る。「10バガー探索は右裾を狙う以上、破綻リスクの高い銘柄も見たい」という立場もありうる。
3. **S-6段階2の方針** — 上限到達銘柄を Tier 2 に回すと、VMET のような1位銘柄がランキングから消える。「モデルの対象外だと分かっている状態にする」（27.21①の方針）とは整合するが、利用者の期待とは食い違う可能性がある。
4. **A-2 を実施するか** — S-4 で解消する可能性がある。先に S-4 を済ませて判断する。
5. **流動性ゲートの再calibration** — `universe.yaml` の `min_daily_dollar_volume_usd` は「単日出来高の代理指標にもとづく暫定値」であり、コメントに「`price_snapshots` 蓄積後に必ず再calibrationすること」と明記されている。**755営業日分たまっているので今すぐ実施できる。** 20〜60営業日の中央値で測り直すと通過銘柄数が変わるため、ユニバース全体に影響する。実施タイミングの判断が要る。

---

## 9. 進捗チェックリスト

**2026-08-26に全項目着手。実装結果と実測にもとづく採用・不採用の判断は §11 を参照。**

### フェーズ1
- [x] B-1 v4スコアのバックフィル（08-23 / 08-24。以後、モデル変更のたびに3日分を再スコアリングして検証に使った）
- [x] KPI基準線のスナップショット保存（§2.2に記録済み）

### フェーズ2
- [x] S-1 粗利率フロアの押し上げ修正（実装・テスト・backtest確認済み。単独でlift 1.272→1.284、rank_ic +0.148→+0.151に改善）

### フェーズ3
- [x] S-6段階1 クランプ到達フラグ（`factors.growth_rate_clamped`）
- [x] C-1 下振れ確率（`probability_below_half`/`probability_below_one`、API実装済み）
- [x] C-4 警告バッジ（バックエンド算出＋フロントエンド表示、7種類の警告コード）
- [x] B-2 / B-3 / B-4 / C-6 表示の注記（RankingPage/TickerDetailPage/ValidationPageに追加）
- [ ] S-6段階2（σを広げる／Tier2へ回す等の挙動変更）は未実施。§8-3の判断待ち

### フェーズ4
- [x] S-5段階1 リース債務の分離と可視化（`lease_share_of_net_debt`、DBIで67%と判明）
- [ ] S-5段階2 挙動変更は**意図的に未実施**。会計上の解釈(2a/2b/2c)が絡み、
      §8-1でオーナー判断を要求していたため、診断のみに留めた

### フェーズ5
- [x] S-3 粗利率系列 → **実装した上でrun-backtestにより不採用と判断**(§11参照。単調性 0.842→0.745に悪化)
- [x] S-2 相対キャップ → 採用（`max_relative_change: 2.0`。1.3/1.5は単調性を悪化させ、2.0のみ全指標で無効時以上だった）
- [x] S-4 正規化粗利 → **実装・採用した後、レビューで撤回**（順位指標は改善したが確率の水準を壊していた:上位5銘柄の平均が 5.0%→13.3%。§11.2参照。S-4が狙った問題はS-1で既に解消済みだった）
- [ ] A-2 判断 → **未実施のまま据え置き**。VMET(EV/粗利98.9x)・BRUN(49.7x)はS-4の対象(粗利率トレンドの歪み)ではなく構造的高評価であり、恣意的な閾値をbacktestなしで入れるのは避けた

### フェーズ6
- [x] S-7 単一観測の上限（実装・テスト・backtest確認済み）
- [x] S-8 ナウキャストの監視KPI(`nowcast_cap_hit_rate`)追加 → **挙動変更(cap引き下げ・反転方向の別上限)は試した上で不採用**(§11参照。単調性を悪化させた)
- [x] A-1 希薄化欠損の中立化（断面中央値へ変更、実装・テスト済み）
- [ ] S-9 生存確率の下限（案2）は未実施。案1(表示)のみ実施。§8-2の判断待ち
- [x] B-5 `delisted_at` の調査と修正（原因特定:yfinanceが404を`info`空返却として握りつぶし`EmptyResponseError`経路に入っていた。連続失敗閾値による自動delisted化を実装）
- [x] B-8 消えた銘柄の非活性化（`refresh_universe`に実装。現存データでは既に自然発生的な隔離で解消済みだったため、コード修正は再発防止用）
- [x] B-6 実行進捗の区別（`run_started`/`run_finished`マーカーをCollectionLogに追加）
- [x] B-7 `invalid_data`→`sanitized`へ改称
- [ ] C-2 / C-3 / C-5 / C-7 は未実施(低優先度のUI追加機能。効果測定を要さないので後回しにしても被害が無い)

---

## 11. 実装フェーズで判明した追加の知見(2026-08-26)

当初の監査(§1〜§8)は静的なコードレビューにもとづく仮説だった。実装時に
`run-backtest` で検証した結果、**2件の仮説が実測で覆った**。これは
「もっともらしく見える修正案でも、必ずKPIで検証する」という本ドキュメント自身の
方針(§0.3)が正しく機能した例として記録する。

### 11.1 S-3(粗利率トレンドを全期間の傾きにする案)は不採用

**仮説**:直近2期の差分だけでは1年だけの変動を過大に外挿する(ALTO型)。
3期以上あれば最小二乗の傾きを使うほうが穏やかで良いはず。

**実測**(2026-08-26、3日分・v4):

| 構成 | 単調性 | リフト | rank_IC | t | 最悪日リフト | 上位デシル破綻率 |
|---|---|---|---|---|---|---|
| 直近2期の差分のみ(基準) | 0.842 | 1.319 | 0.154 | 3.55 | 1.021 | 0.078 |
| **全期間の傾き(S-3案)** | **0.745** | 1.317 | 0.154 | 3.60 | 1.021 | 0.087 |

**結論**:プロジェクトの主指標であるデシル単調性が明確に悪化した。直近の勢いには
それ自体に予測力があり、古い期を均等に重み付けすると薄まる。**したがって
`terminal_gross_margin` は直近2期の差分のみを使う設計へ戻した。** コードには
経緯をコメントで残し、`_linear_slope` 実装は削除した(死んだコードを残さない)。

### 11.2 S-4(粗利率の谷/山によるEV/粗利の歪み補正)も最終的に不採用

**当初の判定は「採用」だった。** 順位指標が単独で改善したためである:

| 構成 | 単調性 | リフト | rank_IC | t |
|---|---|---|---|---|
| 補正なし(基準) | 0.842 | 1.319 | 0.154 | 3.55 |
| S-4補正あり | 0.879 | 1.401 | 0.155 | 3.54 |

**しかし実装後のレビューで、確率の水準を壊していることが判明した。**
順位指標(単調性・リフト・rank_IC)はすべて**順位ベース**であり、確率の
絶対水準には感度が無い。較正誤差も universe 全体の平均を見るため、
上位だけが壊れても検出できない。上位の水準を直接測ると:

| 構成 | 最大確率 | 上位5銘柄の平均 |
|---|---|---|
| 補正なし | 7.61% | **5.00%** |
| S-4補正あり | **21.81%** | **13.32%** |

READMEが明記している「小型株が10倍になる基準率は1%未満なので、上位銘柄でも
数%にしかなりません」という前提を、上位が21.8%になった時点で破っている。

**根本原因は2つ。**

**(1) 二重計上(構造的)。** 恒等式を展開すると、補正の効果は
`terminal_margin ÷ gm_latest` を `terminal_margin ÷ 履歴中央値` に
置き換えることに等しい。つまり**マルチプルの分母だけ正規化して、終端粗利の
予測は正規化していない**という不整合であり、粗利率の改善を二重に数えていた。
実際 `GP_terminal = revenue_latest × revenue_multiple × terminal_margin` は
`gm_latest` に依存しないので、整合的な倍率の分母は正規化前の
`gross_profit_latest` のほうである。

**(2) 「谷」の判定が構造転換に効いてしまう(実装)。** 補正は無制限で、
中央値は3〜5点の短い履歴から取る。赤字から黒字へ構造転換した銘柄では
履歴中央値が「正常値」を表さない:

| 銘柄 | 粗利率の履歴 | 現在 | 補正倍率 | multiple_change |
|---|---|---|---|---|
| ALTO | −2.1%, 1.3%, 1.0% | 3.8% | **3.32x** | 3.00(上限に張り付き) |
| KMTS | **−139.6%**, 1.3%, 40.5% | 51.4% | 2.46x | 1.62 |
| SCZM | 8.2%, 1.3%, 20.2% | 33.5% | 2.36x | 2.07 |

ALTOは `margin_multiple`(2.00=上限)と `multiple_change`(3.00=上限)の
**両方がクランプに張り付いて1位**になっていた——本監査が §1.1 で問題にした
「クランプに当たった銘柄が上位に来る」構造そのものを、S-4が新たに作っていた。

**そもそもS-4が狙っていた問題はS-1で解消済みだった。** 監査時にS-4の根拠と
した AMR(EV/粗利93.9倍を据え置きながら粗利を3倍にしていた)は、その3倍が
S-1のフロア押し上げバグ由来である。S-1修正後の AMR は `margin_multiple` が
4.29 → 1.00 となり、期待倍率1.01・確率0.014%まで落ちた。二重取りは消えている。

**結論:S-4は撤回した。** `margin_trough_correction` と、それだけのために
足していた `MoicInputs.gross_margin_history` の配線(`point_in_time.py` 側の
組み立てを含む)を削除した。S-3・S-4とも不採用になったため、年次粗利率系列を
`scores.inputs` に保存する理由も無くなっている(14.11のストレージ方針)。

**この失敗から得た教訓(次の担当者へ)**:
**順位指標だけでモデル変更を採否判定してはいけない。** 本ドキュメントの
§6.1「モデルを変更したときの必須手順」は `run-backtest` のKPI比較しか
求めていなかったが、それでは確率の水準の破壊を検出できない。
**上位銘柄の確率水準(最大値・上位5平均)を必ず併せて確認すること。**
確認用のSQLを §2.3(d) に追加した。

### 11.3 S-8(ナウキャストの上限を狭める案)も不採用

**仮説**:上位銘柄の3割が`nowcast_cap`(±15pt)に張り付いており、特に決算が
縮小を示す銘柄を成長側へ反転させる補正は一次情報の上書きなので、より狭い上限
(反転方向のみ、または全体でcapを0.10に)を課すべき。

**実測**:

| 構成 | 単調性 | リフト | rank_IC | 最悪日リフト |
|---|---|---|---|---|
| 現状(cap=0.15、反転制限なし) | **0.842** | **1.319** | 0.154 | **1.021** |
| cap=0.10(反転制限なし) | 0.782 | 1.323 | 0.155 | 0.902 |
| 反転方向のみ狭い上限(0.03〜0.08) | 0.76〜0.79 | ほぼ同じ | ほぼ同じ | ほぼ同じ |

**結論**:どちらの対処もKPI(特に単調性と最悪日リフト)を悪化させた。「一次情報を
株価で上書きしている」という仮説診断は正しいが、対処として試した2案は
「成長株の入口を削る」副作用のほうが大きかった。**したがって `nowcast_cap` は
0.15のまま据え置き、`nowcast_cap_hit_rate` という監視KPIの追加のみを採用した**
(`/validation` の`/api/v1/backtest/latest`と `run-backtest` CLI出力に表示)。

### 11.4 累積効果(2026-08-26、最終状態)

採用した変更(S-1・S-2・S-7・A-1)をすべて適用した最終状態のKPI:

| KPI | 監査時点(§2.2) | 最終状態 |
|---|---|---|
| decile_monotonicity | 0.8303 | **0.8424** |
| lift_ratio | 1.2719 | **1.3185** |
| rank_ic | +0.1479 | **+0.1541** |
| rank_ic_t_stat | 3.20 | **3.55** |
| lift_ratio_worst_date | 0.8951 | **1.0209**(28.16の既知の弱点「最悪日がユニバースを下回る」を解消) |
| top_decile_loss_rate | 0.1063 | **0.0779** |
| calibration_error | −0.0502 | −0.0445 |

確率の水準(§2.3(d)):最大 7.58% / 上位5平均 5.00% / 中央値 0.0895%
——READMEの「上位銘柄でも数%」と整合している。

主指標・副指標とも監査時点から改善しており、確率の水準も壊していない。
検討した修正のうち、**実測で悪化したもの(S-3、S-8の挙動変更)と、順位指標は
改善したが確率の水準を壊したもの(S-4)は採用しなかった**。

---

## 12. 変更履歴

| 日付 | 内容 |
|---|---|
| 2026-08-26 | 初版。v4（`scoring_version: v4`、`score_date: 2026-08-25`）の実データ監査にもとづく |
| 2026-08-26 | 実装フェーズ完了。§9チェックリスト更新、§11に実測結果(S-3/S-8不採用、S-4採用の経緯)を追記 |
| 2026-08-26 | 実装後の全体レビュー。**S-4を撤回**(順位指標は改善したが確率の水準を破壊。§11.2に詳細)。併せて `empty_response_delisted` をサーキットブレーカーの失敗集合へ追加、B-8の掃引に大量誤隔離ガードを追加、ランキング画面の「飽和帯」判定がページ先頭行で漏れる不具合を修正。§2.3(d)に確率水準の確認手順を追加 |
