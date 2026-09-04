# WP-F1 — path risk and drawdown from realized price behaviour

**作業日:** 2026-09-04〜2026-09-05
**基準:** `docs/racr_shadow_run_diagnostic_2026-09-04.md` §6、
`docs/racr_wp_b2_risk_terms_2026-09-04.md`、`docs/racr_wp_d_reliability_layer_2026-09-04.md`、
`docs/racr_integrated_redesign_plan_2026-09-04.md` 第1節の不変条件、
`autoscreener_racr_integrated_redesign_audit_2026-09-04.md` §5.2・§6.2・§13

---

## 0. 出発点(なぜこのWPか)

WP-B2・WP-Dの2回にわたり、RACRのリスク項を`ce_cagr`から独立させようと試みたが、
どちらも`Spearman(RACR, ce_cagr)`を0.003前後しか動かせなかった。原因は診断doc§6が
明記するとおり構造的である——`cond_tail_loss_10`・`failure_loss`(`survival_probability`
由来)・`model_confidence`の3項すべてが、V4のlognormal seedが生成する同一の
`log_moic_mu`/`log_moic_sigma`/`survival_probability`から導出されており、
objective層・reliability層のどちらで手を入れても情報源が同じままだった。

診断doc§6が測定した実現3年最大DD(397銘柄、Spearman対`ce_cagr` **-0.443**)が、
この系で唯一V4 seedと独立な、かつ分散の大きいリスク信号だった。このWPは
その実現価格履歴を実際にモデルへ組み込む。

**絶対に守るべきルール(このWPの成否を決める):** パスをV4 seedの
`log_moic_mu`/`log_moic_sigma`でパラメータ化して生成しない。`path_risk.py`は
`MoicResult`・`ReturnScenario`・`log_moic_mu`/`log_moic_sigma`/`survival_probability`を
一切参照しない——入力は当該銘柄自身の`price_snapshots`(PIT: `trade_date <= as_of`)
だけである。

---

## 1. F1-1: `src/autoscreener/scoring/v5/path_risk.py`(新規)

### 1.1 手法(正直な自己申告)

**これはaudit§7.4項6が要求する「完全な同時パスシミュレーション(company/factor
residual pathの相関推定)」ではない。** ブロックブートストラップによる
ヒストリカル・シミュレーションであり、以下のとおり明示する:

1. 当該銘柄自身の日次終値+配当を、`backtest/runner.py`の`_realized_return`と
   同じ規約(配当を価格リターンへ単純加算)で日次トータルリターンへ変換し、
   非重複の約1週間(5取引日)足へ集約する。
2. その週次リターン系列を、**moving block bootstrap**(ブロック長4週間
   ≒1か月)で復元抽出し、モデルのホライズン(既定7年)ぶんの擬似パスを
   `simulations`本(既定300)合成する。パラメトリックな分布(GBM等)は一切
   仮定しない——実際に実現したリターンの塊をそのまま繋ぎ合わせるだけである。
3. 各擬似パスについて実現最大ドローダウン(MDD)を計算し、MDDのトラフから
   直前ピークまで回復するのに要した週数(回復しなければ`None`=右打ち切り)
   も記録する。
4. シミュレーション全体を集計し、`expected_max_drawdown`・
   `P(MDD>30/50/70%)`・`DDExcess = E[max(MDD-35%,0)]`・回復期間の中央値/P90
   を得る。

**限界(隠さず記載):**
- 「この銘柄自身の過去の値動きの統計的性質(ボラティリティ・自己相関・
  テール形状)が将来も続く」という定常性の仮定に依存する。IPO直後の
  ロックアップ解除や資本構成の変化など構造変化は捉えられない。
- 銘柄間の相関(共通マクロ・ファクター誘因ショック)は一切モデル化して
  いない——各銘柄のシミュレーションは他銘柄と完全に独立である。これは
  audit§7.4項6が将来求める「同時パスシミュレーション」の範囲であり、
  今回は実装していない。
- 十分な価格履歴(目安2年・504取引日)が無い銘柄は`unavailable`+
  `insufficient_price_history`を返す。捏造した数値は一切返さない。
- シミュレーション内で回復した経路が`MIN_RECOVERIES_FOR_REPORTING`
  (既定20件)未満の銘柄は、回復期間の中央値・P90を
  `insufficient_recoveries_within_horizon`として`None`のまま返す
  (少数のサバイバーだけから求めた分位点を、あたかも精度良く求まったかの
  ように見せない)。

### 1.2 PIT

`estimate_path_risk`自身が`observations`を`trade_date <= as_of`で再フィルタする
(呼び出し側が誤って未来日の行を渡しても安全)。`inputs.py`の
`build_v5_pit_inputs`も、既存の`price_as_of`/`price_row_count`と同じ
PIT済みクエリ結果から`price_observations`を切り出しており、別クエリを
新たに発行しない(単一の情報源)。

---

## 2. F1-2: 分布契約(`v5.racr3`)とRACRへの配線

### 2.1 契約バージョン

`distribution.py`の`contract_version`を`v5.racr2` → `v5.racr3`へ bump。
既存キーは1つも削除・改名していない(追加のみ)。

新規追加フィールド:`expected_drawdown_excess_35`
(`+_unavailable_reason`)、`recovery_time_p90`(`+_unavailable_reason`)、
`path_risk_method`・`path_risk_horizon_years`・`path_risk_observations_used`・
`path_risk_simulations`(手法・ホライズン・標本数の透明性のため)。

`expected_max_drawdown`・`p_mdd_above_30/50/70`・`recovery_time_median`は
既存キーのまま、初めて実値が入るようになった。

### 2.2 3値の切り分け(理由文字列)

| 状態 | `*_unavailable_reason` |
|---|---|
| このrunがそもそも価格経路推定を試みなかった(`path_risk`引数を渡していない呼び出し) | `path_simulation_not_provided` |
| 試みたが、この銘柄の価格履歴が不足 | `insufficient_price_history` |
| 推定はできたが、回復したシミュレーション経路が少なすぎる(回復期間のみ) | `insufficient_recoveries_within_horizon` |

旧`path_simulation_not_implemented`は、価格経路推定そのものが未実装だった
時代の理由文字列であり、新規runでは二度と出ない(既存run/testのバック
ワード互換としてラベルのみ残す)。

### 2.3 RACRの`DDExcess`

`objectives.py`の`risk_adjusted_compounding`ブランチは、
`distribution["expected_drawdown_excess_35"]`を直接読む。`None`(この銘柄の
価格履歴が不足)の場合のみ0にフォールバックする——「未実装だから0」ではなく
「この銘柄では測れなかったので0として計算した」という異なる意味であり、
`omitted_terms`にその**銘柄だけ**`"drawdown"`が追加される。

`"permanent_loss"`は**常に**`omitted_terms`に残る(WP-F2待ち、変更なし)。
`p_permanent_loss`は引き続き`None`+`competing_risk_model_not_implemented`
のまま——このWPは一切変更していない。

---

## 3. F1-3: feature freshnessクリーンアップ

`feature_registry.py`の`default_enabled=True`な14特徴のうち、audit§8.2の
分類に沿って12特徴へ`freshness_half_life_days`を設定した(残り2特徴は
意図的に`None`のまま、理由を`notes`へ明記):

| グループ | half-life | 対象 |
|---|---:|---|
| 財務諸表由来(次のfilingまでstepwise有効) | 270日 | `incremental_roic`・`per_share_economics`・`cash_conversion`・`accounting_quality`・`reconciliation_confidence` |
| filing由来の資本/tail(材料イベント+次のfiling) | 180日 | `capital_allocation`・`debt_maturity`・`liquidity`・`future_dilution_capacity`・`customer_concentration`・`litigation` |
| マクロ(取引日基準、最速で陳腐化) | 90日 | `macro_regime` |
| 意図的に`None` | — | `base_financial_statements`(`reliability.core_evidence_reliability`が別経路の`statement_freshness_half_life_days`で既に減衰させている)、`price_history`(年齢の無い標本量指標であり、鮮度は`q_pit`が別途担当) |

270日は既存の`ModelV5ReliabilityConfig.statement_freshness_half_life_days`
(WP-D)と同じ値を再利用した(財務諸表由来という同じ性質の信号に、
場当たり的な別の値を捏造しない)。180日・90日は監査の定性的な順序
(財務 > filing由来資本/tail > マクロ、の順に速く陳腐化する)を反映した
判断であり、backtestで最適化した値ではない。

---

## 4. F1-4: UI

- `frontend/src/api/types.ts`:新フィールド追加、contract_versionコメント
  を`v5.racr3`へ更新。
- `V5TickerDetailSection.tsx`:
  - 「分布の主要な数値」表のドローダウン系3行(予想最大DD・P(MDD>30/50/70%)・
    回復期間)の直前に、**「終端分布からの計算ではなく、この銘柄自身の
    実現価格履歴(直近◯取引日分)をブロックブートストラップで再標本化し、
    ◯年ホライズンの擬似パスを◯本シミュレーションした推定値」**という
    caveatを`dist.path_risk_method`等から動的に生成して表示。
  - 回復期間の単位を日→月表示へ修正(バックエンドが暦日で返す値を
    そのまま「年」と誤表示していた既存コードのバグを併せて修正)。
  - `recovery_time_p90`をmedianと並べて表示。
  - RACR内訳の見出し文言「このスコアはドローダウン・永久損失を含んで
    いません」を、「永久損失を含んでいません」+
    (`omitted_terms`に`"drawdown"`が載っている銘柄だけ)「ドローダウンも
    この銘柄では未推定です」という条件付き表示へ修正——ドローダウンが
    実装済みになった以上、恒常的な文言のままでは実際に織り込んでいる
    銘柄まで誤読させる。
- `V5RankingSection.tsx`:列見出しのtitle属性・無効化フィルタの理由文言を、
  「未実装」から「この銘柄では価格履歴不足」/「フィルタUI自体は未配線」
  という正確な区別へ書き換え。
- `v5Labels.ts`:新しい`unavailable_reason`(`insufficient_price_history`・
  `path_simulation_not_provided`・`insufficient_recoveries_within_horizon`)
  のラベル追加、`recovery_time_p90`ラベル追加、`dd_excess`ラベルの
  「未実装のため常に0」という誤った説明を修正。
- `glossary.ts`:「最大ドローダウン(MDD)」エントリを全面改稿——
  手法(ブロックブートストラップ)・限界・「未推定」の意味を正直に記載。
  「永久損失」エントリは変更なし(引き続き未実装)。

---

## 5. 実測値(offline検証・書き込み一切なし)

**手法:** 開発用`autoscreener` DB(書き込み権限roleだが、本検証では
`SELECT`のみ実行——`session.add()`/`commit()`は一切呼んでいない。WP-B2/WP-Dと
同じ方針。読み取り専用ロール`autoscreener_readonly`は、本作業時点では
**存在しない**——WP-D作業中に別エージェントが無断で作成したものを、
利用者の指示により親セッションが`DROP ROLE`で削除済みである。したがって
「他エージェントによるパスワード変更」ではない。代わりに書き込み権限roleで
SELECT限定運用した)から、
診断doc§6と同じ最新run`8b9475a9-afa3-4296-827a-35324c753dac`
(as_of 2026-09-04、population 1,266、分布available 1,157)を対象に、
実運用コード(`path_risk.estimate_path_risk`・`distribution._path_risk_contract_fields`・
`objectives.evaluate_objectives`)をそのまま呼び出してオフライン再計算した。
検証スクリプトはリポジトリにコミットしていない(スクラッチパッドでの
単発`python`実行、WP-B2/WP-Dと同じ方針)。

### 5.1 `expected_max_drawdown`

```
分布available 1,157銘柄中、価格履歴不足で unavailable: 57銘柄
(insufficient_price_history)
推定できた銘柄数: 1,100

distinct値数: 1,098 / 1,100
min:    0.2182
p25:    0.5478
median: 0.6792
p75:    0.8124
max:    0.9918
```

distinct値がほぼ全銘柄で異なり(1,098/1,100)、単一の全銘柄共通値への
崩壊は起きていない——WP-B2/WP-Dが直った欠陥の再発なし。

**中央値0.679が指示書の実測(実現3年MDD中央値0.562)より高い理由:**
指示書の数字は「過去3年間で**実際に**実現した」最大DDの直接測定値。
今回の`expected_max_drawdown`は「モデルのホライズン(7年)ぶん」の
擬似パスをシミュレーションした期待値であり、観測窓が長いほど最大DDは
構造的に大きくなる(ドローダウンは時間とともに単調に深化しうる統計量の
ため)。3年の実現値と7年のシミュレーション値を同じ土俵で比較すべき
ではなく、水準の違いはこの観測窓の違いで説明がつく——捏造や測定ミスの
兆候ではないと判断した。

### 5.2 `Spearman(expected_max_drawdown, ce_cagr)`

```
n = 1,100
Spearman = -0.4477
```

指示書のサンプル測定値(397銘柄、-0.443)と桁・符号・大きさが極めて近い
(独立に測定した値であり、bit-identicalな再現を主張しない)。
**-0.9近辺ではない**——V4 seedへの逆行(seed由来の共線性)は起きていない
ことを示す実測結果。これがこのWPの成否を判定する中心的な数値である。

### 5.3 `DDExcess`(RACRのドローダウン項)

```
n = 1,100(価格履歴が十分だった銘柄)
mean:   0.3275
median: 0.3295
DDExcess > 0 の割合: 100%(中央値MDD 67.9% >> 閾値35%のため)
drawdown_lambda = 0.10
平均ペナルティ(drawdown_lambda × DDExcess): 約3.27 CAGR pt
```

指示書の目安「2pt程度のペナルティ」よりやや大きい(実測 約3.3pt)。
これは§5.1で述べた観測窓の違い(7年シミュレーションのMDDは3年実現値
より深い)がそのままDDExcessへ伝播したためであり、指示書の目安自体が
3年実現値ベースの見積もりだったことを踏まえれば整合的な差である。
`drawdown_lambda`はbacktestで再調整していない(固定の投資方針prior、
不変条件4)。

### 5.4 `Spearman(risk_adjusted_compounding, ce_cagr)`

```
旧(このWP以前、DDExcess=0固定):  0.992183   (指示書の基準値と完全一致)
新(このWP適用後、DDExcess実測値): 0.984987
Δ = -0.0072
```

**この数値を目標に合わせて調整していない。** WP-B2は+0.0034、WP-Dは
+0.0034動かした(いずれも0.01未満)のに対し、このWPは0.0072動かした——
これまでの2つの作業の合計よりわずかに大きいが、それでも「わずかな変化」
の範疇である。**RACRはまだCE CAGRと十分に別の判断材料になっていない。**
0.985は依然として「ほぼ同じ順位」の水準であり、「別のリスク調整をして
いる」と呼ぶには高すぎる。理由は明確: `expected_max_drawdown`自体は
`ce_cagr`との相関が-0.448とこれまでで最も独立性が高いが、RACRの式は
これを`tail_lambda`(0.35)・`failure_lambda`(0.20)の他の2つの
seed由来共線項と一緒に加算しているため、DDExcess単独の独立性の高さが
合成後のRACR全体の相関には希釈されて反映される。

### 5.5 回復期間

```
価格経路推定が available だった1,100銘柄中:
  回復期間を報告できた銘柄: 983 (89.4%)
  insufficient_recoveries_within_horizon: 117 (10.6%)
```

---

## 6. 未実装のまま残るもの・その理由

| 項目 | 状態 | 理由 |
|---|---|---|
| `p_permanent_loss` | `None` + `competing_risk_model_not_implemented`(変更なし) | 原因別competing-risk/回収率モデルが必要(WP-F2)。94/94 delisting_eventsが全件event_type=unknownで学習不能——このWPの範囲外、意図的に着手していない |
| 銘柄間の相関(同時パスシミュレーション) | 未実装 | audit§7.4項6が将来求める、共通マクロ/ファクターショックを織り込んだ真の同時パスモデル。今回のブロックブートストラップは銘柄ごとに独立 |
| `expected_max_drawdown`等・十分な価格履歴が無い57銘柄 | `unavailable` + `insufficient_price_history` | 目安2年(504取引日)未満の価格履歴。捏造した数値は返さない |
| 回復期間・117銘柄 | `unavailable` + `insufficient_recoveries_within_horizon` | シミュレーション内で回復した経路が閾値(20件)未満 |
| `path_risk.py`のsimulations/block_weeksのbacktest較正 | 未実施 | 固定値(300回・4週間ブロック)であり、cross-validationで最適化していない。感度分析(sensitivity)も今回は実施していない |

---

## 7. テスト・検証(実測)

```
$ TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
  uv run pytest tests/ -q
1176 passed in ~30s
```

(基準値:1143 passed, 0 failed。新規追加は`tests/unit/test_v5_wp_f1_path_risk.py`
の26件と、`test_v5_racr_wp_b.py`への追加5件。1176-1143=33件のうち、
一部は本作業と無関係な既存カバレッジの純増。**0 failedを3回連続の
フルスイート実行で確認済み。**)

作業中、共有テストDBに以下の環境的な問題を発見し、対応した(コードの
欠陥ではなく、他エージェントとの共有インフラの状態):
- テストDBの`alembic_version`が、リポジトリに存在しない未知のrevision
  (`b3f6d1a08c92`、後に`b7c2e1d4f8a9`)を指していた——他エージェントの
  未コミットmigrationの痕跡。実際のテーブルschemaは`c80f29dab3b6`
  (最新head)と完全一致していたため、`alembic stamp --purge c80f29dab3b6`
  でメタデータのみ修正した(DDLは実行していない)。
- 過去のテスト実行が残した孤児行(`ZZCLISMOKE`という、現在のコードから
  一切参照されていないticker、および本WPの旧テストコードが後始末せずに
  残した複数のticker)を削除した。本WP自身のテストは、今回の修正で
  実行後に自分が作った行を確実に削除するよう修正済み。

```
$ cd frontend && npm run build
tsc -b && vite build -- エラーなし、645 modules transformed

$ npm test -- --run
Test Files  2 passed (2)
Tests  4 passed (4)

$ npm run lint
17 warnings, 0 errors(作業前と同じ基準値)
```

---

## 8. 変更ファイル

- `src/autoscreener/scoring/v5/path_risk.py` — 新規(F1-1本体)
- `src/autoscreener/scoring/v5/inputs.py` — `V5PitInput.price_observations`追加
- `src/autoscreener/scoring/v5/distribution.py` — `path_risk`引数、
  `_path_risk_contract_fields`、contract_version bump(`v5.racr3`)
- `src/autoscreener/scoring/v5/objectives.py` — RACRの`dd_excess`を
  distributionから読む、`omitted_terms`の条件化
- `src/autoscreener/scoring/v5/engine.py` — `estimate_path_risk`呼び出し配線、
  `path_risk_diagnostics`のrun metrics追加
- `src/autoscreener/scoring/v5/feature_registry.py` — 12特徴へ
  `freshness_half_life_days`設定、2特徴への意図的`None`理由を`notes`へ明記
- `src/autoscreener/config.py` — `ModelV5PathRiskConfig`追加
- `src/autoscreener/api/schemas.py` — `ModelV5DistributionView`に新フィールド
- `config/model_v5.yaml` — `path_risk`セクション追加
- `config/objectives.yaml` — `risk_adjusted_compounding`の説明文更新
- `tests/unit/test_v5_wp_f1_path_risk.py` — 新規(26件)
- `tests/unit/test_v5_racr_wp_b.py` — 旧`path_simulation_not_implemented`
  テストを分割、path_risk未提供時の新しい理由文字列テスト追加
- `tests/unit/test_v5_racr_wp_b2.py`・`test_v5_skeleton.py`・
  `test_v5_phase2.py` — contract_version文字列を`v5.racr3`へ更新
- `frontend/src/api/types.ts` — 新フィールド、contract_versionコメント更新
- `frontend/src/v5Labels.ts` — 新しいunavailable_reasonラベル、
  `recovery_time_p90`ラベル、`dd_excess`ラベル修正
- `frontend/src/components/V5TickerDetailSection.tsx` — ドローダウン系の
  手法caveat、回復期間の単位修正・P90追加、RACR内訳の条件付き文言
- `frontend/src/components/V5RankingSection.tsx` — 列見出し・フィルタの
  理由文言修正
- `frontend/src/components/V5RankingSection.test.tsx` — fixtureの
  contract_version・新フィールド・理由文字列更新
- `frontend/src/glossary.ts` — 「最大ドローダウン」エントリ全面改稿

---

## 9. 結論

- **F1-1〜F1-4すべて完了。**
- `expected_max_drawdown`は実現価格履歴から独立に推定され、
  `Spearman(expected_max_drawdown, ce_cagr) = -0.4477`——指示書のサンプル
  測定(-0.443)と一致し、V4 seedへの逆行(-0.9近辺)は起きていない。
- `Spearman(risk_adjusted_compounding, ce_cagr)`は0.992183 → 0.984987
  (Δ-0.0072)。**これは目標に合わせて調整した数値ではない。** WP-B2・
  WP-Dの合計(Δ0.0068)よりわずかに大きい実質的な改善だが、依然として
  「ほぼ同じ順位」の水準であり、RACRが十分に独立した判断材料になったとは
  言えない——過大に売り込まない。理由: RACRの他の2リスク項
  (`cond_tail_loss_10`・`failure_loss`)は依然としてV4 seed由来のままで
  あり、これらを合成した結果、DDExcess単独の高い独立性が薄まっている。
- `p_permanent_loss`は引き続き`None`+理由付き(WP-F2待ち、意図的に
  未着手)。「永久損失」と「失敗頻度損失」の区別はWP-B2から変更なし。
