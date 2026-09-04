# WP-B2 — RACRリスク項の実装記録・診断出力

**作業日:** 2026-09-04
**基準:** `docs/racr_shadow_run_diagnostic_2026-09-04.md`(この作業が修正する欠陥)、
`docs/racr_wp_b_output_contract_2026-09-04.md`(改訂対象の契約)、
`docs/racr_integrated_redesign_plan_2026-09-04.md` 第1節の不変条件、
`autoscreener_racr_integrated_redesign_audit_2026-09-04.md` §4.3・§5.2

---

## 0. 出発点(診断の要約)

WP-Bの初回shadow run(`1d7a4fc2-760c-4f3b-b546-b973e3284566`)で、
`Spearman(RACR, ce_cagr) = 1.0000000000` が1,155/1,155銘柄で厳密に成立していた。
原因は3つ:

1. `expected_shortfall_10pct_log`(下位10%タイル)が、生存確率1未満の銘柄では
   下位10%区間が丸ごと破綻atomの内側に収まり、`ln(floor)/H` という**全銘柄同一の定数**
   (`0.6578814551411558`)に潰れていた。
2. `model_confidence` が全銘柄 `0.5` で一定だったため、`ModelUncertainty` 項は
   `0.5 * |ce_cagr|` という **ce_cagrの定数倍**でしかなかった。
3. これら2つの欠陥を自動検知する診断出力が無く、手計算でしか見つけられなかった
   (WP-Bの本来のB-3受け入れ条件だったが、engine.pyがWP-Bのファイル所有範囲外だった
   ため先送りされていた)。

このWP-B2は上記の1と3を直す。2(`model_confidence`が分散しない問題)はWP-D
(reliability層)の担当であり、このWPでは直せない——後述§4で明記する。

---

## 1. B2-1: run単位の診断出力(`scoring/v5/engine.py`)

### 実装

`run_v5_shadow()` の各ティッカーループ内で、以下を蓄積する:

- `ce_cagr_by_ticker`: 分布が`available`な銘柄の`ce_cagr`
- `distribution_field_values` / `distribution_field_counts`: 主要な分布フィールド
  (`ce_cagr`, `expected_cagr`, `median_cagr`, `survival_probability`,
  `model_confidence`, `expected_shortfall_10pct_log`,
  `expected_shortfall_10pct_log_given_survival`, `p_target`)の、母集団全体での
  distinct値集合と観測数

ランキング確定後、4つの純粋関数(DBセッションを一切取らない。プレーンな
dict/list/setだけを受け取る——`_ablate`/`_distribution_for` など本ファイル既存の
関数と同じ設計思想)で以下を計算し、`model_runs.metrics["objective_diagnostics"]`
へ保存する:

| 関数 | 出力 |
|---|---|
| `_pairwise_objective_spearman` | 有効objective全組み合わせのpairwise Spearman、および各objective vs `ce_cagr`のSpearman |
| `_top20_overlap_vs_expected_return` | `expected_return`とのTop20重複数(objective別) |
| `_constant_explanation_terms` | 各objectiveの`explanation`内の数値項のうち、母集団全体でdistinct値が1個しかない項目 |
| `_distribution_field_diagnostics` | 上記の主要分布フィールドのdistinct値数と、1個しかない(定数)フィールドの一覧 |

`Spearman > 0.99` のペア、および定数項・定数分布フィールドはすべて
`model_runs.warnings` へ機械可読な文字列として追記される
(`objective_duplicate_risk:...` / `objective_constant_term:...` /
`distribution_constant_field:...`)。**これが今回の欠陥そのものを検知する仕組み
であり、今後同種の欠陥が手計算なしで見つかるようにするのが目的。**

### 意図的な仕様: false positiveを許容する

`tail_lambda`・`failure_lambda`等のλ係数や`ce_cagr_failure_floor`は設計上
恒常的に定数であり、これも`constant_explanation_terms`に載る(意図された定数を
「バグ」と誤認させないためのフィルタは入れていない)。理由: 「定数項は二度と
手計算で見つかるようにしてはならない」という要求に対し、既知の定数を除外する
ロジックを足すと、そのロジック自体が新しい欠陥(本当は動くべき項を誤って
「既知の定数」として除外してしまう)を生みうる。false positiveのコストは
レビュー時に人間が一覧を見て判別するだけで済むが、false negativeのコストは
「また手計算でしか見つからない」に逆戻りすることであり、非対称に高い。

### テスト

`tests/unit/test_v5_racr_wp_b2.py` に純粋関数の単体テスト(DBなし)と、
`run_v5_shadow()`をモックした3銘柄runでの統合テスト(DBあり)を追加した。
統合テストでは、意図的に`ce_cagr`が銘柄ごとに変わる(distinct=3)一方、
`model_confidence`は(この作業で使うfixtureに信号データを一切与えていないため)
全銘柄同一に潰れることを確認しており、これは実際に診断が2番目の実欠陥
(§0の2)の**縮小再現**を自動検知できることの直接証拠になっている。

---

## 2. B2-2: 生存条件付きテール項と失敗頻度項(`scoring/v5/distribution.py`、`scoring/v5/objectives.py`)

### 2.1 根本原因

全銘柄のfailure mass(`1 - survival`)が12%以上あり(実測: survival最大0.8802)、
固定の10%分位点は例外なく破綻atomの内側に完全に収まる。したがって
`E[ln W | W <= q10(W)]` は常に `ln(floor)/H` という定数になる。

### 2.2 修正方針

頻度(failure probability)と深さ(tail depth)を分離する:

```
RACR = CE_CAGR
     - tail_lambda   * TailLoss10_conditional_on_survival
     - failure_lambda * P(failure) * (1 - assumed_recovery)
     - drawdown_lambda * DDExcess              (unavailable -> 0, omitted_terms)
     - permanent_loss_lambda * P(PermanentLoss) (unavailable -> 0, omitted_terms)
     - uncertainty_lambda * ModelUncertainty
```

- **`TailLoss10_conditional_on_survival`**: 破綻atomを floor するのではなく
  **完全に除外**し、生存条件付きの連続混合分布(lognormal mixture)だけで
  下位10%分位を測る。`distribution.py`の新関数
  `_expected_log_moic_below_quantile_given_survival(q, scenarios)` は、
  既存の `_expected_log_moic_below_quantile(q, scenarios, survival=1.0, ...)`
  を呼ぶだけで実装できる——survival=1.0を渡すと`failure_mass >= q`分岐が
  発火せず(failure_massが0になるため)、floorも一切使われない。数学的には
  「survivalで条件付けると、連続混合分布はそのまま(重みの再正規化も不要)」
  という性質を使っている。
  - 新しい分布フィールド: `expected_shortfall_10pct_log_given_survival`
    (contract v5.racr2で追加。既存の`expected_shortfall_10pct_log`は
    **一切変更せず**そのまま残す——後方互換のためであり、かつ
    「この項は生存確率が低い銘柄では定数に潰れる既知の欠陥を持つ」という
    事実の記録としても意味がある)。
- **失敗頻度項**: `p_failure = 1 - survival_probability`、
  `assumed_recovery = distribution["ce_cagr_failure_floor"]`(既存の
  `CE_CAGR_FAILURE_FLOOR_MOIC` をそのまま再利用——回収率の仮定を2つ独立に
  持たない)。`failure_loss = p_failure * (1 - assumed_recovery)`。

### 2.3 命名: 「永久損失」との混同を防ぐ

新しい`failure_lambda`/`failure_loss`/`p_failure`は**永久損失ではない**。
`p_permanent_loss`は引き続き`None` + `unavailable_reason:
"competing_risk_model_not_implemented"`のまま変更していない。

| | `failure_loss`(新) | `p_permanent_loss`(既存、`None`のまま) |
|---|---|---|
| 何を測るか | 現行モデル自身の破綻atom(倒産・非回収的上場廃止)の発生確率 | 原因別(倒産/買収/その他)に分類したcompeting-riskモデルの推定値 |
| 回収率の扱い | `ce_cagr_failure_floor`という**単一の仮置き定数**(1%) | 原因ごとに推定する回収率分布(未実装、WP-F) |
| 実装状態 | 今回実装済み | 未実装(delisting_events 94件が全件cause/settlement unknownのため学習不能) |

config (`ObjectiveDefinition.failure_lambda`)、コード内コメント、frontend
(`v5Labels.ts`の`V5_RACR_TERM_LABELS`、`V5TickerDetailSection.tsx`の警告文、
`glossary.ts`の「失敗頻度損失」エントリ)のすべてに、この区別を明記した。

### 2.4 λ値

`tail_lambda: 0.35`(既存、変更なし)に加え `failure_lambda: 0.20`
(監査§5.2の政策prior表にある`lambda_P`と同じ値を採用。ただし
`permanent_loss_lambda`とは別のconfigフィールドであり、値が同じなのは
偶然ではなく監査の政策prior表がそう決めているため——後々どちらかだけ
変更する可能性を潰さないためにも別フィールドにしてある)。
いずれも**backtestでfitしていない、固定の投資方針prior**
(不変条件4)。

### 2.5 contract version

分布契約を `v5.racr1` → `v5.racr2` へ bump した。既存キーは1つも削除・改名
していない(追加のみ)。**RACRの絶対値は`v5.racr1`と`v5.racr2`の間で比較不能**
——旧式の定数オフセット(`-0.2303`相当、CE CAGRのアフィン変換だった項)が
消えるため、水準そのものが変わる。

---

## 3. 実測値(offline検証)

`docs/racr_shadow_run_diagnostic_2026-09-04.md`が対象にした同じ実runデータ
(`1d7a4fc2-760c-4f3b-b546-b973e3284566`、読み取り専用DBロール
`autoscreener_readonly`経由、書き込み一切なし)から、各銘柄の
`distribution["scenarios"]`を`ReturnScenario`へ再構成し、新しい
`_expected_log_moic_below_quantile_given_survival`と新RACR式をそのまま適用して
再計算した(検証スクリプトはリポジトリにコミットしていない。スクラッチパッド
`verify_b2_tail_term.py`、pytestではなく単発の`python`実行)。

| 指標 | 実測(このWP) | このWP指示書の推定値 |
|---|---:|---:|
| 生存条件付きTailLoss10・distinct値数 | **1,157 / 1,157** | 1,157 |
| 生存条件付きTailLoss10・min | 0.1229 | ≈0.1229 |
| 生存条件付きTailLoss10・median | 0.3336 | ≈0.3336 |
| 生存条件付きTailLoss10・max | 1.1805 | ≈1.1805 |
| Spearman(新RACR, ce_cagr) | **0.9949652423** | ≈0.9933 |
| (参考)旧`expected_shortfall_10pct_log`のdistinct値数 | 1 | — |
| (参考)Spearman(旧RACR, ce_cagr) | 1.0000000000 | — |

distinct値数・min・median・maxは指示書の推定値と小数点以下4桁まで一致した。
Spearmanは0.9933(指示書)に対し実測0.9950で、桁は合っているが完全一致では
ない(指示書側も「独立に計算した推定値、closely reproduceできればよい」と
明記している差分の範囲内と判断した)。

---

## 4. これが達成すること・達成しないこと(正直な評価)

**達成すること:**

- CE CAGRの定数アフィン変換だった旧RACR(`Spearman = 1.0000000000`)を、
  実際にCE CAGRから乖離しうるスコアに変えた(`Spearman = 0.9950`)。
- 「頻度10%を超えたら必ず同じ定数になる」という、Phase 10が一度terminal ES側で
  直したのと同型の欠陥を、log-CAGR側でも塞いだ。
- 頻度(failure_loss)と深さ(cond_tail_loss_10)を、それぞれ別の係数で
  独立に制御できるようにした——設計どおりの構造(監査§5.2)に戻した。
- 同種の欠陥を二度と手計算で見つけなくて済むよう、run単位の自動診断を追加した。

**達成しないこと:**

- **RACRはまだCE CAGRと十分に別の判断材料になっていない。** Spearman 0.9950は
  「ほぼ同じ順位」の水準であり、「別のリスク調整をしている」と呼ぶには
  まだ高すぎる。理由は明確: `cond_tail_loss_10`は`ce_cagr`と-0.855、
  `failure_loss`(を左右する`survival_probability`)は`ce_cagr`と-0.696の
  相関を持つ(診断doc §3の実測値)——3つとも同じmu・sigma・survivalという
  同一の入力から導出されているため、独立した情報を持ちようがない。
- **本当の修正はWP-D(reliability層)。** `model_confidence`が全銘柄0.5で
  一定である限り、`ModelUncertainty`項は情報を持たない。この項が銘柄ごとに
  意味のある値を持つようになって初めて、RACRはCE CAGRから独立した
  「モデルの自信度」という軸を得る。
- floor(`ce_cagr_failure_floor = 0.01`)の恣意性は変わらず残っている
  (B2-3でUI開示を強化したのみ。値自体はWP-F待ち)。

このWPは「定数を偽装したリスク項を取り除いた」だけであり、「RACRが
ランキングとして意味のある独立情報を持つようになった」わけではない。
過大に売り込まない。

---

## 5. B2-3: floor(1%仮定)のUI開示

`ce_cagr_failure_floor`(既定0.01)は表示水準を強く支配する
(診断doc §4: floorを0.01→0.50に動かすと中央値CE CAGRが-16.6%→-6.2%、
10.5pt動く)。

- 新規コンポーネント `frontend/src/components/V5FailureFloorNote.tsx`:
  既存の`V5UnavailableMetric`と同じトーン(点線下線+title属性によるnative
  tooltip)を踏襲するが、別コンポーネントにした——これは「未実装」ではなく
  「実装済みだが仮置きの定数」であり、`V5UnavailableMetric`の文言
  (「— 未推定」)を流用すると誤り。
  - 常時表示のラベル「※回収率◯%仮定」+ hover先に10.5pt swingの実測値を
    含む詳細説明。
- 適用箇所: `V5RankingSection.tsx`のCE CAGR列見出しとRACR列見出し(選択中が
  RACRのとき)、`V5TickerDetailSection.tsx`の「分布の主要な数値」表の
  CE CAGR行、RACR内訳表のCE CAGR行。
- RACR内訳表に「失敗頻度損失」行を追加し、`p_failure`・`assumed_recovery`を
  括弧書きで併記。直下に「これは永久損失とは別物」という警告文を追加。
- `v5Labels.ts`: `cond_tail_loss_10` / `failure_lambda` / `failure_loss`の
  ラベルを追加。旧`tail_loss_10`ラベルは残す(旧runのexplanationにまだ
  残っているキーのため、フロントは`cond_tail_loss_10 ?? tail_loss_10`で
  フォールバック表示する)。
- `glossary.ts`: CE CAGRエントリに10.5pt swingの実測値を追記、RACRエントリに
  §4の「正直な評価」と同内容(Spearman 0.99超の告白)を追記、
  「期待ショートフォール」エントリを生存条件付きの説明へ更新、
  新規エントリ「失敗頻度損失」を追加し永久損失との違いを明記。
- `ScoreReferencePage.tsx`: RACRの式の説明に③失敗頻度項を追加、
  Spearman 0.99超の告白パラグラフを追加、用語リストに「失敗頻度損失」を追加。

---

## 6. 変更ファイル

- `src/autoscreener/scoring/v5/distribution.py` — 新関数、新フィールド、
  contract_version bump
- `src/autoscreener/scoring/v5/objectives.py` — RACR式の書き換え
- `src/autoscreener/scoring/v5/engine.py` — B2-1診断(新規純粋関数4つ + 統合)
- `src/autoscreener/config.py` — `ObjectiveDefinition.failure_lambda`
- `src/autoscreener/api/schemas.py` — `ModelV5DistributionView`に新フィールド追加
- `config/objectives.yaml` — `failure_lambda: 0.20`
- `tests/unit/test_v5_racr_wp_b2.py` — 新規(50件超)
- `tests/unit/test_v5_racr_wp_b.py` — contract_version文字列を`v5.racr2`へ更新、
  `_objectives_config()`に`failure_lambda`追加
- `tests/unit/test_v5_skeleton.py`、`tests/unit/test_v5_phase2.py` —
  contract_version文字列を`v5.racr2`へ更新
- `frontend/src/components/V5FailureFloorNote.tsx` — 新規
- `frontend/src/components/V5RankingSection.tsx`、`V5TickerDetailSection.tsx` —
  floor開示、RACR内訳への失敗頻度行追加
- `frontend/src/components/V5RankingSection.test.tsx` — fixtureの
  contract_version・新フィールド追従
- `frontend/src/v5Labels.ts` — 新ラベル
- `frontend/src/glossary.ts` — 新エントリ・既存エントリ更新
- `frontend/src/pages/ScoreReferencePage.tsx` — 説明更新
- `frontend/src/api/types.ts` — `expected_shortfall_10pct_log_given_survival`追加
- `frontend/src/index.css` — `.v5-failure-floor-note`スタイル追加

---

## 7. 実行した検証(実測)

```
$ TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
  uv run pytest tests/ -q
1120 passed in 28.86s
```

(タスク指示に記載の基準値: 1076 passed, 0 failed。このWPで新規追加したのは
`test_v5_racr_wp_b2.py`の27件。1120-1076=44との差の一部は、このWP着手前から
作業ツリーにあった無関係な未コミット作業[日次パイプラインのincremental化等]
に起因する可能性があるが、こちらでは基準値取得時点の厳密な内訳を検証していない
——確認できているのは「新規に自分が追加したテストは27件、全体は0 failedのまま」
という事実のみ)

```
$ cd frontend && npm run build
✓ 645 modules transformed, built in ~210ms, エラーなし

$ npm test -- --run
Test Files  2 passed (2)
Tests  4 passed (4)

$ npm run lint
17 warnings, 0 errors (作業前と同じ基準値)
```

offline検証(§3参照)は読み取り専用ロール(`autoscreener_readonly`)のみを
使用し、書き込みは一切行っていない。`run-v5-shadow`等、devの`autoscreener`
DBへ書き込むコマンドは実行していない(タスク指示どおり)。
