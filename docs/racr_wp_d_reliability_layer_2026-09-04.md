# WP-D — canonical feature / reliability layer

**作業日:** 2026-09-04
**基準:** `docs/racr_shadow_run_diagnostic_2026-09-04.md`(この作業が修正する欠陥)、
`docs/racr_wp_b2_risk_terms_2026-09-04.md` §4(「本当の修正はWP-D」)、
`docs/racr_integrated_redesign_plan_2026-09-04.md` 第1節の不変条件、
`autoscreener_racr_integrated_redesign_audit_2026-09-04.md` §7.3(reliability
weightの仕様)・§8.1(欠損)・§8.2(鮮度)・§2.1(confidenceをalphaから分離)

---

## 0. 出発点

`model_confidence` が**全銘柄で厳密に0.5**だった(スコア対象1,157銘柄、
distinct=1)。原因は診断済み:

1. `engine.py` の `base_confidence` が `model_config.reliability.ready_input_confidence`
   という定数(0.50)をそのまま使っていた。
2. `growth.py`/`quality.py`/`balance_sheet.py`/`tail_risk.py` の
   `confidence_delta` は減点のみで、対象信号のほとんどが
   `runtime_enabled=False`(coverage gate未達)のため実質デルタ0。
3. `available_from` は収集日(ingestion date)であり鮮度シグナルではない
   (実測:age min 0, median 0, p90 0, max 1日 — 全銘柄でほぼ定数)。

このWPはこれを直す。

---

## 1. 実装した内容(D-1〜D-4)

### D-1: 新規 `src/autoscreener/scoring/v5/reliability.py`

監査§7.3の式をそのまま実装した:

```
r_x = q_source × q_extract × q_PIT × q_sample × q_reconcile × freshness(age)
freshness(age) = exp(-ln2 · age / halfLife_x)
```

- `freshness(age_days, half_life_days)` — 純関数。age/half_lifeが不明なら
  1.0(ペナルティなし、という明示的な中立値)。
- `reliability_weight(...)` — 上記式そのもの。全`q_*`を`[0,1]`にクランプ。
- `CoreEvidenceReliability` / `core_evidence_reliability(item, as_of, config)` —
  **全銘柄に存在する**証拠(財務諸表・価格履歴)からr_xを計算する:
  - `q_pit`:価格系列の鮮度(as_of時点で価格が古い=フィード障害の疑い)。
  - `q_sample`:年次期数(target 4期)と価格行数(target 756行=約3年)の
    充足度の平均。
  - `q_extract`:直近年次期の必須10項目(revenue, gross_profit,
    operating_income, net_income, operating_cash_flow, free_cash_flow,
    cash_and_equivalents, total_debt, shares_outstanding, total_assets)の
    充足率。
  - `q_source`:`raw_snapshots.is_valid`/`validation_errors`(既存の
    `validation/rules.py`検証結果、新しい数値を捏造していない)。
  - `freshness(age)`:**年次決算のperiod_endからas_ofまでの日数**
    (`available_from`ではない — トラップ1参照)。half_life=270日
    (config `reliability.statement_freshness_half_life_days`)。
- `base_confidence_for(item, as_of, config, has_distribution)` —
  `min_base_confidence + (max_base_confidence - min_base_confidence) *
  core_evidence_reliability.value`(既定 0.10〜0.90)。`has_distribution=False`
  のときのみ旧来の`unavailable_input_confidence`(0.0)にフォールバックする。

**`q_reconcile`は意図的に不活性。** 名前付き定数
`Q_RECONCILE_INERT = 1.0`として全銘柄同一のまま残した。理由:
`xbrl_facts`は291/5,893銘柄しかカバーしておらず(2026-09-04時点)、
全銘柄向けの正直なreconciliation品質シグナルを作れない。
`quality.py`の`reconciliation_confidence`信号は既にXBRL比較可能な
銘柄でmodel_confidenceを直接減点しており、これとは別軸の追加係数として
`q_reconcile`は「広いカバレッジの信号ができるまで不活性」という事実を
明示的に残した(暗黙に1へ畳み込んでいない)。

### D-2: confidenceは証拠で上がり、staleness/欠損で下がる

`reliability.feature_confidence_delta(signals)` を新設し、
growth/quality/balance_sheet/tail_riskの4つの`confidence_delta`
プロパティが共有する(旧実装は4箇所に同じロジックがコピーされていた)。

- 罰則(既存契約のまま):`runtime_enabled`な信号が
  `NOT_COLLECTED`/`COLLECTION_FAILED`なら減点。
- **加点(新規)**:`applied`(実際にstateへ入った証拠)な信号は、その
  信号自身の`reliability`に比例して加点。

監査§2.1の「低confidenceはmeanを下げてはならない」は堅持している:
`scenario.py`の`build_scenarios`はconfidenceに対して**mean-preserving**
(既存のdocstringどおり、conditional meanはconfidenceの影響を受けない)。
confidenceが動かすのは分散(scenario mixtureのsigma)と、RACRの
`ModelUncertainty`項だけであり、`expected_return`(算術平均ベースの
objective)は一切変化しない——本WPはこの構造を変更していない。

### D-3: registryメタデータの実行化・削除

検証済みのdead metadata 4項目のうち:

- **`freshness_half_life_days`**:実行化した。
  `reliability.decayed_reliability(signal, half_life_days, as_of)`を
  `build_growth_feature_sets`/`build_quality_feature_sets`/
  `build_capital_feature_sets`/`build_tail_feature_sets`の各集約ループへ
  配線し、gate判定(`below_min_reliability`)と実際の状態更新の両方に
  効くreliability値へ反映した。現在この値を設定しているのは
  `consensus_revision`(90日)と`guidance`(180日)の2件のみ——他は
  `None`(無減衰)のままであり、これも正直に記録する(捏造した半減期を
  入れていない)。
- **`transform`・`winsorization`・`sector_normalization`**:**削除した**。
  実行への配線ではなく削除を選んだ理由:これらの信号の多く
  (`incremental_roic`のshortfall、`per_share_economics`のgap等)は既に
  「ハードルレート相対の差分」や「有界比率」という単位の変換値であり、
  それをさらにwinsorize/sector z-scoreするには各シグナルの単位定義を
  作り直す必要がある。これは監査自身が別ファイル
  (`scoring/v5/feature_graph.py`、再設計計画のP3)として切り出している
  「重複防止DAG・group aggregation・相関処理」の一部であり、WP-Dの
  reliability層へ浅く継ぎ足すと各下流計算の意味を無断で変えるリスクの
  方が大きいと判断した。`min_reliability`・`required_coverage`は
  (指示どおり)変更していない。

### D-4: `ModelFeatureValue`(per-feature層の永続化)

- `db/models.py`に`ModelFeatureValue`を追加:
  `(run_id, ticker_id, feature_key)`一意、`value`/`source`/
  `coverage_status`/`status`/`applied`/`reliability`/`missing_reason`/
  `observed_at`/`evidence`を保持。
- Alembic migration `c80f29dab3b6_model_feature_values.py`
  (`down_revision = a6d8e0f2b4c6`、既存headへチェーン、新headは
  `c80f29dab3b6`)。
- `engine.py`の`run_v5_shadow`が、growth/quality/capital/tailの全信号
  (16種)+ 常時存在する2つのベース特徴(`base_financial_statements`
  `price_history`、`core_evidence_reliability`由来)を、tickerごとに
  1行ずつ`model_feature_values`へ書き込む。既存の`model_scores.features`
  (nested JSONB)は変更せず維持し、こちらは検索可能な射影として並存する。

**このmigrationは開発用`autoscreener` DBへは未適用。** テストDB
(`autoscreener_test`)にのみ適用済み(`alembic upgrade head`実行済み、
head=`c80f29dab3b6`)。dev DBへの適用は別途の運用ステップとして残す
(本WPの制約:dev DBへ書き込みしない)。

---

## 2. 測定値(実測・読み取り専用)

**手法:** 開発用`autoscreener` DB(書き込み権限roleだが、本検証では
`SELECT`のみ実行——`session.add()`/`commit()`は一切呼んでいない)から、
最新のv5 shadow run `4df5d170-4f0f-4304-a28f-040e0a9cdfeb`
(as_of 2026-09-04、population 1,266、スコア対象1,157)を対象に、
実運用コード(`build_v5_pit_inputs`・`build_*_feature_sets`・
`engine._distribution_for`・`objectives.evaluate_objectives`)をそのまま
再利用してオフライン再計算した。検証スクリプトはリポジトリに
コミットしていない(WP-B2と同じ方針:スクラッチパッドでの単発`python`
実行、pytestではない)。

### 2.1 `model_confidence`(修正前・実測・DB永続値)

```sql
select confidence, count(*) from model_scores
where run_id = '4df5d170-4f0f-4304-a28f-040e0a9cdfeb'
group by confidence;
-- confidence=0.00000: 109行(distribution unavailable)
-- confidence=0.50000: 1157行(distribution available)
```

| 指標 | 値 |
|---|---:|
| distinct(スコア対象1,157銘柄) | **1** |
| min / p25 / median / p75 / max | 0.5 / 0.5 / 0.5 / 0.5 / 0.5 |
| 持続的なRACR vs CE CAGR Spearman(永続値、修正前コード) | **0.9949652422694651** |

これは指示書に記載の「現在0.5」「Spearman 0.994965」と完全一致する。

### 2.2 `model_confidence`(修正後・実測・オフライン再計算)

同じ1,157銘柄・同じ`MoicResult`・同じgrowth/quality/capital/tail
feature setsに対し、`base_confidence`だけを新しい
`reliability.base_confidence_for(...)`へ差し替えて再計算した
(それ以外は`engine._distribution_for`をそのまま呼んでいるため、
confidenceの計算経路以外は本番コードと同一)。

| 指標 | 値 |
|---|---:|
| n | 1,157 |
| distinct | **175** |
| min | 0.165411 |
| p25 | 0.542330 |
| median | 0.560330 |
| p75 | 0.578330 |
| max | 0.842213 |

内訳:`core_evidence_reliability.value`(D-1のr_x本体)自体の分布:

| 指標 | 値 |
|---|---:|
| n | 1,157 |
| distinct | **102** |
| min | 0.036764 |
| p25 / median / p75 | 0.530412 / 0.530412 / 0.530412 |
| max | 0.844141 |

p25/median/p75が同一値に見えるのは、多くの銘柄が
「四半期決算の典型的な報告ラグ」+「q_source/q_extract/q_sampleが
いずれも1.0近辺」という同じクラスタに集中しているため(報告ラグは
9種類の離散値にクラスタする、と指示書に明記されている実測どおり)。
distinct=102は、この報告ラグの離散クラスタに加え、`q_extract`
(必須10項目の充足数、0〜10の離散値)と`q_sample`(価格本数・年次期数の
連続比率)の組み合わせで生じている。

### 2.3 optional信号(growth/quality/capital/tail)のreliability分布

`runtime_enabled=True`だった全信号(coverage gateを通過した信号のみ、
1,157銘柄×16特徴のうち実際にgateを通過したもの)を集計:

| 指標 | 値 |
|---|---:|
| n | 3,917 |
| distinct | **16** |
| min | 0.128013 |
| p25 / median / p75 | 0.900 / 0.900 / 0.900 |
| max | 0.900 |

これは**意図した挙動**である。これらの信号の多くは
`_STATEMENT_RELIABILITY = 0.90`(財務諸表由来、Phase 4)という
固定値を使っており、そもそも連続分布を意図していない(文字列tierの
`_reliability()`マッピングは`{manual, high, medium, low, unknown}`の
5値のみ)。**D-1が新規に連続分散を作ったのは`core_evidence_reliability`
(§2.2)であり、既存のoptional信号reliabilityは今回変更していない。**
median/p75が0.90なのは、Phase 4のfinancial-statement系信号
(`incremental_roic`/`per_share_economics`/`cash_conversion`/
`accounting_quality`)が712〜1,082銘柄と最も広いcoverageを持ち、
この母集団の大半を占めるため。

### 2.4 `Spearman(risk_adjusted_compounding, ce_cagr)`

| 経路 | n | Spearman |
|---|---:|---:|
| DB永続値(修正前コード、本番run) | 1,157 | **0.994965** |
| オフライン再計算・旧confidence(0.5固定、方法論の整合性確認用) | 1,157 | 0.995540 |
| オフライン再計算・新confidence(D-1〜D-3適用後) | 1,157 | **0.992183** |

再計算した「旧confidence」列(0.995540)は永続値(0.994965)と桁は
一致するが完全一致ではない——WP-B2の診断doc §3と同じ理由
(独立に再計算した値であり、bit-identicalな再現を主張しない)。

**結論を誇張しない:** 新confidenceでSpearmanは0.994965 → 0.992183
(再計算ベースでは0.995540 → 0.992183、Δ≈0.0034)しか動いていない。
これは「わずかな変化」であり、RACRがCE CAGRから大きく独立した
判断材料になったとは言えない。理由は明確: RACRの他の2項
(`cond_tail_loss_10`・`failure_loss`)も、model_uncertaintyも、
すべて同じmu・sigma・survivalという同一の入力から導出されており、
confidenceを正しく分散させても、それ単独では「別の情報源」には
ならない。**confidenceを誠実な証拠品質の指標にすることが目的であり、
Spearmanを目標値に近づけるための調整は一切行っていない**
(調整すれば0.05のuncertainty_lambdaや0.10のmin_base_confidenceを
動かして相関を下げることもできたはずだが、それはしていない——これらの
値はaudit§7.3・§8.1の仕様と、この母集団のreporting lag実測クラスタ
から導いた値であり、Spearmanを見て後付けで選んだものではない)。

---

## 3. 意図的に不活性・未実装のまま残したもの

| 項目 | 状態 | 理由 |
|---|---|---|
| `q_reconcile` | 定数`Q_RECONCILE_INERT = 1.0`(全銘柄) | XBRL coverage 291/5,893銘柄(5%)しかなく、全銘柄向けの正直な値を作れない |
| `transform`/`winsorization`/`sector_normalization`(FeatureSpec) | **削除**(実行化ではない) | 各信号が既に「ハードルレート相対差分」等の変換済み単位を持ち、これをさらにz-score/winsorizeするには各シグナルの単位を作り直す必要がある大きな設計判断。監査自身がP3(`feature_graph.py`)として別work化している範囲 |
| `freshness_half_life_days` | 実行化したが、設定されているのは`consensus_revision`(90日)・`guidance`(180日)のみ | 他14特徴は今回半減期を追加していない(捏造しない) |
| optional信号(growth/quality/capital/tail)自体のreliability | 変更なし(既存の文字列tierマッピング、または`_STATEMENT_RELIABILITY=0.90`固定) | D-1の対象は「全銘柄に存在する証拠」からのconfidence算出であり、個別信号のreliability定義自体の再設計はWP-Dの範囲外 |
| `model_feature_values`マイグレーション | テストDBのみ適用、開発用`autoscreener` DBは未適用 | 本WPの制約(dev DBへ書き込み禁止) |

---

## 4. テスト・検証

```
$ TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
  uv run pytest tests/ -q
1143 passed in 29.71s
```

(基準値:1120 passed, 0 failed。新規追加は`tests/unit/test_v5_wp_d_reliability.py`
23件。1143-1120=23で一致。既存テスト2件を仕様変更に合わせて更新した:
`test_v5_phase3_growth.py`の`consensus_revision`鮮度テスト用日付
[freshness decayが新たに効くようになったため]、
`test_v5_skeleton.py`の`test_v5_shadow_persists_separately_without_touching_v4`
[flat 0.5固定の期待値を、証拠皆無フィクスチャでの`min_base_confidence`
期待値へ更新]。)

新規テストの主な内容:

- `reliability.py`の純関数群(`freshness`/`reliability_weight`/
  `core_evidence_reliability`/`base_confidence_for`/
  `feature_confidence_delta`/`decayed_reliability`)の単体テスト。
- **回帰ガード**:`test_run_v5_shadow_model_confidence_varies_with_real_evidence`
  —証拠の異なる3銘柄で`run_v5_shadow`を実行し、
  `model_confidence`のdistinct値が3であることを assert する
  (`len(confidences) == 3`が失敗すれば、まさに今回の欠陥の再発)。
  併せて`run.metrics["reliability_diagnostics"]`・
  `ModelFeatureValue`永続化(base_financial_statements/price_history
  含む)も検証する。
- `FeatureSpec`から`transform`/`winsorization`/`sector_normalization`
  が消えたこと、`freshness_half_life_days`は残っていることの契約テスト。

フロントエンドは変更していない(API契約・型は不変:`model_confidence`は
既存のfloat 0-1のまま)。`npm run build`/`npm test`/`npm run lint`は
未実行(変更対象外のため)。

---

## 5. 変更ファイル

- `src/autoscreener/scoring/v5/reliability.py` — 新規(D-1/D-2/D-3の中核)
- `src/autoscreener/scoring/v5/inputs.py` — `V5PitInput`に
  `sector`/`price_row_count`/`price_first_date`/`raw_is_valid`/
  `raw_validation_error_count`追加
- `src/autoscreener/scoring/v5/growth.py`・`quality.py`・
  `balance_sheet.py`・`tail_risk.py` — `confidence_delta`を
  `reliability.feature_confidence_delta`委譲へ置換、
  `build_*_feature_sets`へfreshness decay配線
- `src/autoscreener/scoring/v5/feature_registry.py` — `transform`/
  `winsorization`/`sector_normalization`削除
- `src/autoscreener/scoring/v5/engine.py` — `base_confidence_for`配線、
  `_feature_value_rows`・`_value_summary`追加、
  `run.metrics["reliability_diagnostics"]`追加
- `src/autoscreener/config.py` — `ModelV5ReliabilityConfig`に
  `min_base_confidence`/`max_base_confidence`/
  `statement_freshness_half_life_days`/`target_annual_periods`/
  `target_price_history_rows`追加
- `config/model_v5.yaml` — 上記の既定値
- `src/autoscreener/db/models.py` — `ModelFeatureValue`追加
- `alembic/versions/c80f29dab3b6_model_feature_values.py` — 新規
  (`down_revision=a6d8e0f2b4c6`)
- `tests/unit/test_v5_wp_d_reliability.py` — 新規(23件)
- `tests/unit/test_v5_phase3_growth.py` — consensus鮮度テストの日付調整
- `tests/unit/test_v5_skeleton.py` — flat-confidence期待値の更新
