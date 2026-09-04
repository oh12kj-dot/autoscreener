# WP-B — RACR output contract(P1)実装記録

**作業日:** 2026-09-04
**担当:** sonnet(隔離worktree `agent-a87ff9914ecf4840a`)
**基準計画:** `docs/racr_integrated_redesign_plan_2026-09-04.md` 第3節(B-1〜B-4)
**基準監査:** `autoscreener_racr_integrated_redesign_audit_2026-09-04.md` §4.3, §5, §6

---

## 0. スコープと不変条件の遵守

- `config/objectives.yaml` の `default_objective` は `ten_bagger` のまま変更していない。
- `risk_adjusted_compounding`(RACR)は `enabled: true` の shadow objective として追加した。default化はしていない。
- 既存 `risk_adjusted` は `deprecated: true` を付けて残し、削除も無効化もしていない。
- Permanent Loss / Drawdown 系フィールドは一貫して `None` + `unavailable_reason` 文字列。0を返す箇所は無い(後述§3で全項目を列挙)。
- 既存の distribution/API契約キーは1つも削除・改名(旧キー削除の意味で)していない。追加のみ。

変更したファイル(計画の「Files you own」の範囲内):

- `config/objectives.yaml`
- `src/autoscreener/scoring/v5/distribution.py`
- `src/autoscreener/scoring/v5/objectives.py`
- `src/autoscreener/config.py`(`ObjectiveDefinition`/`ObjectivesConfig` のみ)
- `src/autoscreener/api/schemas.py`(`ModelV5DistributionView` のみ)
- `tests/unit/test_v5_racr_wp_b.py`(新規)
- `tests/unit/test_v5_phase2.py`, `tests/unit/test_v5_skeleton.py`(§5「既存テストへの影響」参照。distribution契約バージョン文字列の一致先を更新しただけで、テスト自体の検証内容は変えていない)

`engine.py`、`routes.py`、`api/main.py`、`batch/`、`pipeline_stages.py`、`cli.py`、`tests/conftest.py` は一切変更していない。

---

## 1. B-1: version分離

- `scoring/v5/distribution.py` の distribution contract版を `v5.phase2` → `v5.racr1` へ変更した(`scenario_distribution`・`unavailable_distribution` 両方)。
- `state_model.py` 側の独立した `contract_version`(`v5.phase2`/`v5.phase6` 表記、state用)には一切触れていない。distributionとstatesは別のcontract_versionフィールドであり、今回はdistributionのみを対象にした。
- コード中に `contract_version == "v5.phase2"` のような分岐ロジックは存在しないことを確認済み(grep実施)。文字列は単なる記録用フィールドであり、後方互換を壊す読み手は無い。
- `base_distribution()`(Phase 1のレガシーserializer、`v5.phase1.base`)は対象外。今回のスコープは分布契約(Phase 2以降の本体)のみ。

---

## 2. B-2: 分布からの追加出力

すべて既存と同じ `scenarios`/`survival` から導出。新しい確率モデルは導入していない。

| 新フィールド | 定義 | 備考 |
|---|---|---|
| `ce_cagr` | `exp(E[ln W_H]) - 1`。破綻atomは `ce_cagr_failure_floor` でfloor | §4参照 |
| `ce_cagr_failure_floor` | floor値(定数 `0.01`) | 常に記録。`unavailable`時はNone |
| `p_cagr_above_15` / `_20` / `_25` | `P(W_H > (1+r)^H)`、`r=0.15/0.20/0.25`。閾値は`horizon_years`から都度計算 | 定数MOICを埋め込んでいない |
| `expected_shortfall_10pct_log` | `E[ln W_H \| W_H <= Quantile_{0.10}(W_H)] / H`。`ce_cagr`と同じfloorを使用 | 既存 `expected_shortfall_10pct`(MOICベース)と併記。値は変更していない |
| `p_terminal_wealth_below_0_5` | 旧 `p_moic_below_0_5` の改名(エイリアス)。旧キーは値も含めて完全に残存 | ラベル変更のみ。「永久損失」ではなく「大幅元本毀損確率」の意 |
| `p_permanent_loss` | 常に `None` | 理由: `p_permanent_loss_unavailable_reason = "competing_risk_model_not_implemented"` |
| `expected_max_drawdown` | 常に `None` | 理由: `expected_max_drawdown_unavailable_reason = "path_simulation_not_implemented"` |
| `p_mdd_above_30` / `_50` / `_70` | 常に `None` | 各々に対応する `_unavailable_reason` = `"path_simulation_not_implemented"` |
| `recovery_time_median` | 常に `None` | 理由: `recovery_time_median_unavailable_reason = "path_simulation_not_implemented"` |

### 恒等式テスト(実施済み、`tests/unit/test_v5_racr_wp_b.py`)

- quantile単調性: `p10 <= p25 <= p50 <= p75 <= p90`
- `p_moic_2x >= p_moic_3x >= p_moic_5x >= p_moic_10x`
- `p_moic_below_1_0 + P(W>1) == 1`
- CAGR↔MOIC threshold変換(H=7): 15%→2.660x、20%→3.583x、25%→4.768x、10x→38.950%/yr。すべて監査記載の数値と一致することを数値的に確認
- `median_cagr == p50_moic ** (1/H) - 1`
- `p_terminal_wealth_below_0_5 == p_moic_below_0_5`(改名が値を変えていないことの直接確認)
- `p_cagr_above_15 >= p_cagr_above_20 >= p_cagr_above_25`

---

## 3. `unavailable` として明示出荷している項目、と外す条件

以下は**現時点でも将来的にも0を返してはならない**フィールドである。値を持たせるには、それぞれ別のWork Packageでモデル実装が必要になる時点まで `None` + 固定の `unavailable_reason` 文字列を返し続ける。

| フィールド | `unavailable_reason` | 外す条件 |
|---|---|---|
| `p_permanent_loss` | `competing_risk_model_not_implemented` | 破綻/上場廃止のcause別 competing-risk モデルと、原因別recovery分布が実装される(監査§5.3、計画WP-F)。現状 `delisting_events` 94件全件がcause/settlement unknownであり、学習不能 |
| `expected_max_drawdown` | `path_simulation_not_implemented` | 相関を持つfactor/company path simulationが実装される(計画WP-F)。現行分布は終端(H年後)一時点のみのモデルであり、保有中経路を持たない |
| `p_mdd_above_30` | `path_simulation_not_implemented` | 同上 |
| `p_mdd_above_50` | `path_simulation_not_implemented` | 同上 |
| `p_mdd_above_70` | `path_simulation_not_implemented` | 同上 |
| `recovery_time_median` | `path_simulation_not_implemented` | 同上 |

`unavailable_distribution()`(分布自体が計算不能な場合)でもこれら全フィールドを `None` にしている。ただし理由文字列はそちらでは `None` のままにした — 「distribution自体が使えない」ことは既存の `status: "unavailable"` が既に表しており、そこへさらに「未実装」という別の理由を重ねると、どちらが実際の原因か読み手が誤認しかねないため(コード内コメント参照)。

---

## 4. `ce_cagr` の破綻atom floor

**問題:** 破綻/非回収的上場廃止は現行モデルでMOIC=0の一点質量として表現される。`E[ln W_H]` はこの質量に `ln(0) = -inf` を掛けるため、生存確率が1未満のあらゆる銘柄(実質すべて)で `CE_CAGR` が数学的に負の無限大へ発散する。

**採用した対応:** 破綻atomのMOICを `CE_CAGR_FAILURE_FLOOR_MOIC = 0.01`(元本の1%回収)でfloorしてから対数を取る。

```python
E[ln W_H] = (1 - survival) * ln(0.01) + survival * Σ_i weight_i * log_mu_i
```

**floor値の根拠:** 計画書が明示する例示値(「例:0.01x」)をそのまま採用した。0という値そのものではなく、「回収率分布が実装されるまでの間、破綻時に元本の99%を失うと仮定する」という**保守的だが恣意的なプレースホルダ**である。これは推定でも較正値でもない。この事実を隠さないよう、`ce_cagr_failure_floor` を分布契約の一級フィールドとして常時記録し(computed available時は`0.01`、`unavailable`時は`None`)、`RACR` objectiveのexplanationにも同じ値を`ce_cagr_failure_floor`として転記している。将来、原因別recovery分布(計画WP-F)が実装されたら、この定数floorは撤去し、実測recovery率の期待値に置き換える。それまでは `ce_cagr` の絶対水準(特に生存確率が低い銘柄)を「実際の期待複利リターン」として字義通り読んではならない — ランキングの相対比較にのみ用いる値である。

同じfloorを `expected_shortfall_10pct_log`(下位10%の対数CAGR)にも適用した。生存確率だけで下位10%質量を超える銘柄(`1-survival >= 0.10`)では、下位10%区間全体が破綻atomの内側に入るため、値は厳密に `ln(0.01)/H` となる。テスト `test_expected_shortfall_10pct_log_uses_the_same_floor_as_ce_cagr` で確認済み。

---

## 5. B-3: RACR objective

`config/objectives.yaml` に `risk_adjusted_compounding` を `enabled: true`(shadow)で追加した。`default_objective` は変更していない。

```
RACR = CE_CAGR
     - tail_lambda * TailLoss10
     - drawdown_lambda * DDExcess       (= 0、常に)
     - permanent_loss_lambda * P(PermanentLoss)  (= 0、常に)
     - uncertainty_lambda * ModelUncertainty
```

- `TailLoss10 = max(0, -expected_shortfall_10pct_log)` — distribution契約から直接読む(objectives.py内で再計算しない。既存の`risk_adjusted`が`expected_moic_given_loss`を直接読む設計と同じ思想)。
- `DDExcess`・`P(PermanentLoss)` は**恒久的に0として計算**するが、`explanation["omitted_terms"] = ["drawdown", "permanent_loss"]` を毎回付与する。これはハード制約であり、テスト `test_racr_explanation_always_reports_omitted_terms` で毎回の評価に付くことを確認した。RACRスコアが「ドローダウン・永久損失込みで調整済み」と誤読されることを防ぐためのものであり、値としての0とdistribution契約の`None`(§3)は別概念であることをコード内コメントに明記した。
- `ModelUncertainty = (1 - model_confidence) * abs(ce_cagr)`。`model_confidence` から導出せよという計画の指示に従い、distributionが既に持つsigma(dispersion)を再利用しない設計にした — sigmaは既に`ce_cagr`と`TailLoss10`双方に効いており、そこをもう一度掛けると二重計上になるため。`model_confidence=1.0`で罰則0、`model_confidence=0.0`で`CE_CAGR`の絶対値全体を1標準誤差相当とみなす、という線形補間。
- λ初期値(計画§5.2表と同一): `tail_lambda=0.35`、`drawdown_lambda=0.10`、`permanent_loss_lambda=0.20`、`uncertainty_lambda=0.50`。backtestでfitしていない(config.pyのコメントに明記)。
- 既存 `risk_adjusted` は `config/objectives.yaml` で `deprecated: true` を追加した。`enabled` は変更していないため、引き続きAPIから選択可能・引き続き計算される(champion比較用)。

### 実施できなかった診断出力(計画§3 B-3の一部)

計画は「RACR と `expected_return` の Spearman、および Top20重複数を run metrics へ保存する」ことをB-3の受け入れ条件に含めている。これは横断的(全銘柄一括)な統計量であり、単一distributionを受け取る `evaluate_objectives()`(objectives.py)では計算できない。この集計は `scoring/v5/engine.py`(run全体を扱う層)でしか実装できないが、`engine.py` は本WPの「Files you own」に含まれておらず、かつタスク指示で明示的にこの一点(engine.py, pipeline_stages.py, cli.py, batch/)への変更を避けるよう指示されている。**この診断出力の実装は未着手のまま持ち越す。** WP-C以降(API/UI反映フェーズ)かengine.py側の別作業で実施する必要がある。

---

## 6. B-4: API後方互換

`api/schemas.py` の `ModelV5DistributionView` に、上記の新フィールドをすべて `float | None = None` / `str | None = None`(nullable, デフォルトNone)として追加した。既存フィールドは1つも削除・型変更していない。

検証(pure-Python、`python -c` で実施、pytestではない):

1. 新フィールドを含まない旧形式の辞書(Phase 2以前に永続化されたであろう最小構成)を `ModelV5DistributionView(**old_style)` へ渡し、エラー無くパースでき、新フィールドは全て `None` になることを確認。
2. `scenario_distribution()` が返す新形式のフル辞書を同モデルへ渡し、`ce_cagr`・`p_cagr_above_15`・`p_permanent_loss_unavailable_reason` などが正しく型付けされて読めることを確認。

`api/routes.py` の `_v5_distribution_payload()` は `dict(score.distribution)` をそのまま展開して返す実装であり(コード確認済み、変更不要)、`scenario_distribution()`/`unavailable_distribution()` が返す新キーは自動的にAPIレスポンスへ伝播する。`routes.py` 自体は変更していない。

`/api/v1/models/v5/objectives` エンドポイント(DBセッション不要、`list_v5_objectives()`)を対象に既存テスト `test_v5_objectives_endpoint_excludes_disabled_objectives` を単体実行し、`default_objective` が引き続き `ten_bagger` であること、既存の無効化ロジックが壊れていないことを確認した(下記§7参照)。

---

## 7. 実行したテストと結果

**制約:** `tests/conftest.py` はまだテスト用DBを強制分離していない(WP-Aで並行対応中)。このworktreeはそのDBを共有しているため、**DBセッションを使うテストは一切実行していない**。実行したのはすべてpure-Python(distribution/objectives計算)またはDBセッション不要な単発のFastAPIルートのみ。フルの `pytest` スイートは実行していない。

実行コマンドと結果(実測):

```
$ PYTHONPATH=<worktree>/src python -m pytest tests/unit/test_v5_racr_wp_b.py tests/unit/test_v5_phase10_reliability_objectives.py -v
======================== 57 passed in 0.87s ========================
```

内訳: `test_v5_racr_wp_b.py`(新規、本WP追加分)46件全て成功、`test_v5_phase10_reliability_objectives.py`(既存Phase 10回帰、無変更)11件全て成功(リグレッション無し)。

```
$ PYTHONPATH=<worktree>/src python -m pytest "tests/unit/test_v5_phase8_ui_endpoints.py::test_v5_objectives_endpoint_excludes_disabled_objectives" -v
======================== 1 passed in 1.33s ========================
```

この1件のみDBセッション不要と確認した上で単独実行(同ファイル内の他テストはsession_scope()を使うDB統合テストのため未実行)。

**編集したが実行していないテスト:** `tests/unit/test_v5_phase2.py`(1箇所)・`tests/unit/test_v5_skeleton.py`(1箇所)の `contract_version` 期待値リテラルを `"v5.phase2"` → `"v5.racr1"` へ更新した。両ファイルとも `session_scope()`/`run_v5_shadow()` を使うDB統合テストであり、このworktreeからは実行していない。値の対応関係(旧バージョン文字列→新バージョン文字列)のみを機械的に追随させたもので、テストの検証内容自体は変更していない。フルスイート実行時にWP-A側のDB隔離が整った後、これらが green になることを別途確認する必要がある。

**環境上の注記:** このworktreeの `.venv` は `C:\AI\App_Dev\AutoScreener\.venv` を共有しており、素の `python`/`pytest` はデフォルトで **メイン worktree** の `src/autoscreener` をインポートしてしまう(editable installのpthがメインworktreeを指しているため)。本WPのテスト実行はすべて `PYTHONPATH=<このworktreeのsrc>` を明示して、このworktree内の変更が実際に検証されていることを都度確認した上で行った(`autoscreener.config.__file__` 等で実測確認済み)。

---

## 8. 未実施・持ち越し事項

1. **run metricsへのSpearman/Top20重複診断(§5「実施できなかった診断出力」参照)。** `engine.py` 所有範囲外のため未実装。WP-Cまたは別セッションでの対応が必要。
2. **`tests/unit/test_v5_phase2.py`・`test_v5_skeleton.py` のDB統合テスト自体の実行確認。** WP-AのテストDB隔離が完了してから、フルスイートで green を確認する必要がある。
3. **frontend型・UI表示。** 計画のWP-Cスコープであり、本WPでは対象外(`frontend/` は未変更)。
