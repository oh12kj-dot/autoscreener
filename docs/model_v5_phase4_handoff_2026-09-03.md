# Model v5 Phase 4 以降 引継ぎ書（2026-09-03）

**正本:** GitHub Issue [#3](https://github.com/oh12kj-dot/autoscreener/issues/3)（将来リターン分布・投資目的関数・ランキングの全面再設計）
**引継ぎ先:** Sonnet
**前提文書:** [model_v5_phase0_baseline_2026-09-02.md](docs/model_v5_phase0_baseline_2026-09-02.md) / [model_v5_phase1_skeleton_2026-09-03.md](docs/model_v5_phase1_skeleton_2026-09-03.md) / [model_v5_phase2_distribution_contract_2026-09-03.md](docs/model_v5_phase2_distribution_contract_2026-09-03.md) / [model_v5_phase3_growth_expectations_2026-09-03.md](docs/model_v5_phase3_growth_expectations_2026-09-03.md)
**基準:** `main` = `7f05790`（Phase 0 のみコミット済み）＋ **未コミットの作業ツリー**（Phase 1–3 の実装一式）

---

## 0. 一行結論

**Phase 0–3 は実装済み・実DB検証済みだが、Phase 1–3 は未コミットのまま作業ツリーにある。**
Sonnet の作業は次の3段。

1. 作業ツリーの現状確認 → Phase 1–3 をコミット（新規実装を書き足す前に必ず）
2. **Phase 4（Quality / Accounting / Reinvestment）** を、Phase 3 が確立した実装契約（§3）をそのまま踏襲して実装
3. 実DB shadow run で証拠を取り、`docs/model_v5_phase4_quality_<日付>.md` に Phase 1–3 と同じ体裁で記録

v4 は champion のまま。v5 は shadow challenger であり、Phase 4 では**昇格判断をしない**（昇格判断は Phase 7–9）。

---

## 1. 現在地

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | baseline / data quality（TAM単位バグ、M&A unknown、macro PITフラグ、coverage bias監査） | **完了・コミット済み**（`7f05790`） |
| 1 | v5 skeleton（config / registry / PIT input / `model_runs`・`model_scores` / shadow stage） | **完了・未コミット** |
| 2 | distribution contract（state objects / scenario mixture / distribution outputs / objectives / API） | **完了・未コミット** |
| 3 | growth / TAM / operating KPI / consensus / guidance | **完了・未コミット** |
| **4** | **quality / accounting / reinvestment** | **未着手（本引継ぎの本題）** |
| 5 | capital allocation / balance sheet / debt maturity / liquidity / future dilution | 未着手 |
| 6 | tail / macro regime / competing risk（M&A・litigation・customer concentration） | 未着手 |
| 7 | backtest / champion–challenger / ablation / bootstrap / regime / bias | 未着手 |
| 8 | UI（model selector / objective selector / v4対v5 / state説明 / validation画面） | 未着手（frontend に v5 参照は**0件**） |
| 9 | Promotion Decision Record | 未着手 |

### 未コミット変更の内訳

| 種別 | パス |
|---|---|
| 新規モジュール | [src/autoscreener/scoring/v5/](src/autoscreener/scoring/v5/)（`inputs.py` `feature_registry.py` `growth.py` `scenario.py` `state_model.py` `distribution.py` `objectives.py` `engine.py`） |
| 新規 config | [config/model_v5.yaml](config/model_v5.yaml) / [config/objectives.yaml](config/objectives.yaml) |
| 新規 migration | `alembic/versions/f0a1b2c3d4e5_model_v5_phase1_tables.py` / `alembic/versions/1d2e3f4a5b6c_model_v5_phase2_objectives.py` |
| 既存改変 | `config.py`（v5 config モデル） / `db/models.py`（`ModelRun` `ModelScore` `ObjectiveScore`） / `api/routes.py`・`api/schemas.py`（`/api/v1/models/v5/*`） / `cli.py`（`run-v5-shadow`） / `pipeline_stages.py`・`batch/daily_pipeline.py`（stage `model_v5_shadow`=23） |
| 新規テスト | `tests/unit/test_v5_skeleton.py` / `test_v5_phase2.py` / `test_v5_phase3_growth.py` |
| Phase 0 の追記分 | `batch/collect_investment_intelligence.py`・`screening/investment_intelligence_extract.py`（XBRL異常値の債務抽出リジェクト）ほか |

**実DBのマイグレーションは既に `1d2e3f4a5b6c` まで適用済み**（Phase 1・2 の doc に upgrade/downgrade 検証記録あり）。コミット前に `uv run alembic current` で head 一致を再確認すること。

### 残っている運用ブロッカー（Issue §31。Phase 7 の前に解消が要る）

- v4 backtest は全KPIが `INSUFFICIENT_DATA`（有効評価日が 5.31 / 1.21 日しかない）
- delisting settlement が 0%（上場廃止銘柄の決着が backtest に入っていない）
- coverage bias 監査が `REVIEW_REQUIRED`（Spearman 0.825、上位decileほど Live データが揃っている）
- M&A: `delisting_events` 94件が全部 `unknown` → acquisition competing risk は Phase 6 でも**投入不可**
- macro: `macro_exposure_snapshots` 全行が `fred_vintage_supported=false` → historical backtest 不可、forward shadow のみ

Phase 4 自体はこれらに依存しないので着手してよいが、**「v5 の方が良い」と主張するのは Phase 7 まで禁止**。

---

## 2. コード地図（Phase 4 で触る所）

| ファイル | 役割 | Phase 4 での扱い |
|---|---|---|
| [v5/inputs.py](src/autoscreener/scoring/v5/inputs.py) | PIT入力。`universe_snapshots(included)` の当日母集団 → `RawSnapshot.available_from <= as_of` と `PriceSnapshot.trade_date <= as_of` だけを読む。現在値フォールバック無し | **要拡張**（財務履歴を載せる、§4.2） |
| [v5/feature_registry.py:88-91](src/autoscreener/scoring/v5/feature_registry.py#L88-L91) | 全 signal の契約（target_state / transform / required_coverage / PIT / min_reliability）。`incremental_roic` `accounting_quality` は登録済み・無効 | **要更新**（coverage 実測後に閾値決定） |
| [v5/growth.py](src/autoscreener/scoring/v5/growth.py) | Phase 3 の signal builder + state 更新。**Phase 4 の雛形はこれ** | 変更しない（参照のみ） |
| [v5/scenario.py:25](src/autoscreener/scoring/v5/scenario.py#L25) | seed → 3シナリオ lognormal mixture。confidence は σ だけ広げ、平均は保存 | **要拡張**（σ・左裾の追加係数、§4.5） |
| [v5/state_model.py:103](src/autoscreener/scoring/v5/state_model.py#L103) / [:134](src/autoscreener/scoring/v5/state_model.py#L134) | 型付き future state。`economics.reinvestment_efficiency` は `_unsupported("phase4")` | **要実装**（この穴を埋めるのが Phase 4） |
| [v5/distribution.py](src/autoscreener/scoring/v5/distribution.py) | 分布出力契約（P(loss)/2x/3x/5x/10x/expected・median MOIC・CAGR・ES10・P10–P90） | 原則変更不要 |
| [v5/objectives.py](src/autoscreener/scoring/v5/objectives.py) | 同一分布から目的関数を計算。`quality_compounder` `execution_adjusted` は未実装 | 判断は §4.9 |
| [v5/engine.py:142-260](src/autoscreener/scoring/v5/engine.py#L142-L260) | shadow runner。feature set 構築 → 分布 → **leave-one-out ablation** → 永続化 | **要拡張**（ablation ループの共通化） |
| [config.py](src/autoscreener/config.py) | `ModelV5Config` 系 pydantic | **要追加**（`ModelV5QualityConfig`） |

補助（読むだけ）:
[investment_intelligence.py:28](src/autoscreener/scoring/investment_intelligence.py#L28) `calculate_reinvestment_quality()` / [accounting_quality.py:25](src/autoscreener/screening/accounting_quality.py#L25) `calculate_accounting_quality()` / [financial_history.py:256](src/autoscreener/screening/financial_history.py#L256) `build_financial_history()` / `validation/reconciliation.py` `reconcile()` / [routes.py:3524-3557](src/autoscreener/api/routes.py#L3524-L3557)（reinvestment endpoint の NOPAT・IC 代理定義）/ [routes.py:3626-3652](src/autoscreener/api/routes.py#L3626-L3652)（accounting endpoint）。

---

## 3. Phase 3 が確立した実装契約（Phase 4 でも必ず守る）

Issue の要求をコードへ落とした結果であり、ここを崩すと Phase 3 の検証がやり直しになる。

1. **PIT境界は1本**。`_cutoff(as_of)` = `as_of + 1日 00:00 UTC` 未満（[growth.py:123](src/autoscreener/scoring/v5/growth.py#L123)）。`observed_at` / `reported_at` / `announced_at` / `period_end` の全部に効かせる。現在値を過去に使わない。
2. **universe coverage gate**。母集団全体の `collected_with_data` 率が `FeatureSpec.required_coverage` 未満なら、その feature は**行を持つ銘柄も含め全銘柄で無効**（`status="runtime_disabled_low_coverage"`）。取得対象であること自体をランク優位にしない（[growth.py:383-404](src/autoscreener/scoring/v5/growth.py#L383-L404)）。
3. **欠損は平均を動かさない**。`not_collected` / `collection_failed` は state を変えず `confidence` のみ下げる（-0.03 / -0.08、上限 ±0.20）。逆に「観測が有る」だけの confidence ボーナスも与えない（[growth.py:72-84](src/autoscreener/scoring/v5/growth.py#L72-L84)）。
4. **適用した feature には必ず leave-one-out ablation を保存**。`state_shift` / `scenario_impact`（p_target・expected_cagr の差分）/ `without_feature` を記録し、計算していないものは `{"status":"not_computed","reason":...}` を必ず入れる（捏造したゼロ影響を書かない）。全銘柄 × 全 feature の枠を必ず埋める（[engine.py:199-248](src/autoscreener/scoring/v5/engine.py#L199-L248)）。
5. **更新は config で有界**。重みも上限も `config/model_v5.yaml`。ハードコード定数を作らない。
6. **registry と feature_flags は整合必須**。`validate_feature_flags()` が未知キーで落ちる。registry payload は `v5_config_hash()` に入るので、**registry を変えると config hash が変わる＝別 run 扱い**になる（比較時に注意）。
7. **v4 に触らない**。`scores` テーブルは読み書きしない。`model_runs` / `model_scores` / `objective_scores` は append-only。shadow stage の失敗が v4 production を止めない。
8. **confidence は「良い会社か」ではなく「自信を持って推定できるか」**。`ready_input_confidence = 0.50` が出発点。混同しない。

---

## 4. Phase 4 実装指示（Quality / Accounting / Reinvestment）

Issue §6 が正本。要求は「incremental ROIC・per-share economics・cash conversion・accounting quality・reconciliation confidence を、**加点ではなく state と不確実性**へ接続する」こと。

### 4.1 新規ファイル `src/autoscreener/scoring/v5/quality.py`

`growth.py` と**同型の API** にする（engine 側の扱いを揃えるため）。

```python
@dataclass(frozen=True)
class QualitySignal:      # growth.GrowthSignal と同じフィールド構成
    key: str; status: str; coverage_status: str; runtime_enabled: bool
    applied: bool; reliability: float; observed_at: datetime.datetime | None
    value: float | None; evidence: dict

@dataclass(frozen=True)
class QualityFeatureSet:  # confidence_delta / applied_keys / excluding(key) / to_dict()
    signals: tuple[QualitySignal, ...]
    universe_coverage: dict[str, float]

@dataclass(frozen=True)
class QualityUpdate:
    duration_multiplier: float        # growth_duration への作用（<=1.0 で短縮）
    mean_multiplier: float            # 売上成長→1株価値の変換効率（<=1.0 で減衰）
    sigma_multiplier: float           # >=1.0。accounting quality はここに効く
    left_tail_extra: float            # >=0.0。左裾だけ重くする追加分
    confidence_penalty: float         # reconciliation 不一致など
    applied_keys: tuple[str, ...]
    signal_effects: dict[str, dict]

def build_quality_feature_sets(session, items, *, as_of, config) -> dict[int, QualityFeatureSet]: ...
def apply_quality_features(result, features, *, config, excluded_key=None) -> QualityUpdate: ...
```

### 4.2 データ源と PIT（重要）

Phase 4 の主入力は **`raw_snapshots.payload` の年次財務**であり、Phase 3 のような Live Intelligence テーブルではない。

- `V5PitInput` は現在 `raw_snapshot_id` しか持たず payload を持たない。**`inputs.py` を拡張して `financial_history`（`build_financial_history(raw.payload)` の結果のうち `period_end <= as_of` の年次のみ）を載せる**のが推奨。理由: PIT境界の判定箇所を1つに保てる／`raw_snapshots` を二度引かない。
- 年次が2期未満なら `insufficient_annual_history` として `not_collected` 相当（[routes.py:3536-3538](src/autoscreener/api/routes.py#L3536-L3538) と同じ判定）。
- NOPAT・投下資本の代理定義は既存 endpoint と同一にする（`operating_income * (1 - tax_rate)`、IC = `total_debt - cash_and_equivalents`）。ただし**税率 0.79 のハードコードは config 化**し、純関数へ切り出して endpoint と v5 で定義が食い違わないようにする。
- `calculate_reinvestment_quality()` / `calculate_accounting_quality()` は**再利用する**（新しい計算式を書き起こさない）。

**accounting quality の既知の穴（Issue と現行コードの差分。証拠付きで doc に記録すること）:**
`FinancialPeriod`（[financial_history.py:56-71](src/autoscreener/screening/financial_history.py#L56-L71)）は revenue / gross_profit / operating_income / net_income / OCF / capex / FCF / cash / total_debt / net_debt / shares しか持たない。よって現状 `calculate_accounting_quality()` に渡せるのは `net_income` `operating_cash_flow` `revenue` `revenue_growth` だけで、`average_assets`（accrual ratio）・receivables・inventory・SBC・goodwill は `None` になる（[routes.py:3646-3650](src/autoscreener/api/routes.py#L3646-L3650) が実際にそうなっている）。

選択肢は2つ。**取れないものを推定で埋めないこと。**

- (a) `_build_periods()` に `Total Assets` / `Inventory` / `Accounts Receivable` / `Goodwill`（balance_sheet）と `Stock Based Compensation`（cash_flow）の行を**追加オプショナルフィールドとして**足す。`FinancialPeriod` の生成箇所は [financial_history.py:163](src/autoscreener/screening/financial_history.py#L163) の1箇所・キーワード引数のみなので追加は安全。ただし v4 共有コードなので、**既存値が1つも変わらない**ことをテストで示すこと。
- (b) Phase 4 では `cash_conversion`（OCF/NI・FCF/NI）だけを使い、accrual/SBC/goodwill 系は `not_collected` と明示して Phase 5 以降へ送る。

推奨は (a)。ただし取得できなかった行は `None` のまま `not_collected` で通し、0 で代替しない。

### 4.3 signal → state の接続表（加点は禁止）

| key | 入力 | 接続先 state | 効かせ方 |
|---|---|---|---|
| `incremental_roic` | ΔNOPAT / ΔIC（ΔIC>0 のときのみ） | `growth.duration_years` | 高成長 × 低 incremental ROIC のとき **duration を短縮**（`duration_multiplier < 1`）。高ROICでも duration を無条件に伸ばさない（上限は horizon） |
| `per_share_economics` | 全社 CAGR と 1株 CAGR の乖離（revenue / gross profit / FCF） | `growth` の平均倍率 | 乖離が大きい（＝発行や M&A で規模だけ増えた）ほど `mean_multiplier` を減衰。v4 の dilution drag が既に効いているので `capital.diluted_share_factor` と**二重計上しない**（重複回避の根拠を doc に明記） |
| `cash_conversion` | OCF/NI・FCF/NI | `economics.cash_conversion`, `economics.reinvestment_efficiency` | 現在 `_unsupported("phase4")` の穴を実値で埋める。悪化は平均ではなく分布の質へ |
| `accounting_quality` | accrual ratio / cash conversion 劣化 / receivables・inventory の売上超過 / SBC / goodwill | **`uncertainty`** | `sigma_multiplier` 増・`left_tail_extra` 増。**平均を下げない**（Issue §6.3） |
| `reconciliation_confidence` | `reconcile()` の `MISMATCH` / `MAGNITUDE_MISMATCH`（`XbrlFact.filed_date <= as_of`） | `uncertainty.model_confidence` | confidence のみ下げる。state は動かさない |

`per_share_economics` / `cash_conversion` / `reconciliation_confidence` は registry に未登録。**`FEATURE_REGISTRY` に追加し、`config/model_v5.yaml` の `feature_flags` にも同じキーを足す**（片方だけだと `validate_feature_flags()` で落ちる）。

### 4.4 コーナーケース（テストで固定すること）

- ΔIC <= 0 → incremental ROIC は `None`（無限大や符号反転を作らない）
- NOPAT <= 0 / 赤字企業 → reinvestment rate は `None`。「赤字＝悪」として mean を下げない
- `shares_outstanding` 欠損 → per-share CAGR は `None`（全社CAGRで代用しない）
- NI ≒ 0 の cash conversion → 分母ガード。極端値は winsorize してから使う
- 期首・期末の間隔が短すぎる/長すぎる → `years` の外挿を信用しない（endpoint は `max(1.0, days/365.25)`）
- 通貨換算不可（`FinancialHistory.currency_conversion_unavailable`）→ 比率系のみ使用、金額の比較はしない

### 4.5 scenario / distribution への渡し方

`build_scenarios()` に σ と左裾の追加パラメータを足す。**既定値で Phase 2/3 と数値が完全一致**すること（既存テストがそのまま回帰ガードになる）。

```python
def build_scenarios(result, *, confidence, config,
                    conditional_mean_multiplier: float = 1.0,
                    sigma_multiplier: float = 1.0,
                    left_tail_extra: float = 0.0) -> tuple[ReturnScenario, ...]:
```

平均保存の正規化（`normaliser`）は維持する。σ を広げたあとも条件付き期待値が seed と一致することをテストで固定する。

### 4.6 config 追加

`config/model_v5.yaml`:

```yaml
implementation_version: v5.phase4     # pattern ^v5\.phase\d+$
quality:
  nopat_tax_rate: 0.21                # NOPAT 代理の税率（現行 0.79 係数の逆）
  min_annual_periods: 2
  incremental_roic_weight: <小さく始める>
  per_share_gap_weight: <小さく始める>
  max_duration_reduction_years: <上限>
  accounting_sigma_max_multiplier: <上限。例 1.5>
  accounting_left_tail_extra_max: <上限>
  reconciliation_confidence_penalty: <上限>
  ablation_enabled: true
feature_flags:
  incremental_roic: true
  accounting_quality: true
  per_share_economics: true
  cash_conversion: true
  reconciliation_confidence: true
```

`config.py` に `ModelV5QualityConfig`（`ModelV5GrowthConfig` と同様に `Field(ge=..., le=...)` と `model_validator` で境界を検証）を追加し、`ModelV5Config.quality` に載せる。

### 4.7 registry の coverage 閾値

Phase 3 と同じ順序で決める。**まず実DBで coverage を測り、その実測値を根拠に閾値を書く**（希望値を先に書かない）。財務諸表由来なので高いはずだが、`insufficient_annual_history` の実数を数えてから決めること。reconciliation は `xbrl_facts` の PIT 可用率次第で低い可能性が高く、その場合は Phase 3 の TAM/KPI と同様に **coverage gate で全銘柄無効になるのが正しい結果**（無理に通さない）。

### 4.8 engine.py の変更

- `build_quality_feature_sets()` を `build_growth_feature_sets()` と並べて呼ぶ
- confidence は growth と quality の delta を合算してから `[0,1]` にクランプ
- **ablation ループを growth 用のまま複製しない**。`_ablate(key, ...)` のような共通ヘルパへ抽出し、growth/quality どちらのキーでも leave-one-out を計算できるようにする（現行 [engine.py:199-248](src/autoscreener/scoring/v5/engine.py#L199-L248) をコピペすると二重管理になる）
- `metrics` に quality 分の `applied_feature_counts` / `feature_universe_coverage` を追加
- `_score_warnings()` の `phase3_growth_features_shadow_only` を Phase 4 の文言へ更新（`not_for_production` は残す）
- `build_future_state(..., quality_update=...)`、`contract_version="v5.phase4"`、`economics.reinvestment_efficiency` の `_unsupported("phase4")` を除去

### 4.9 objectives（`quality_compounder`）

Phase 4 の state が揃えば有効化を検討してよいが、Issue §18.4 の通り**別の100点サブスコアに戻さない**。分布出力を中心に構成し、quality は分布経由で既に効いている点を踏まえて二重計上しない。有効化するなら `config/objectives.yaml` と `objectives.py` の両方を更新し、**有効化した理由（または見送った理由）を Phase 4 doc に書く**。判断は実装後の実データを見てから。

### 4.10 テスト `tests/unit/test_v5_phase4_quality.py`

`tests/unit/test_v5_phase3_growth.py` の構成（純関数テスト＋`session_scope` を使う実DB統合テスト＋`monkeypatch` での engine テスト）を踏襲する。最低限:

- accounting quality の悪化が **σ だけを広げ、条件付き平均を下げない**
- 欠損（`not_collected` / `collection_failed`）が state を動かさず confidence のみ下げる
- coverage gate が「行を持つ銘柄」でも feature を無効化する
- PIT: `available_from > as_of` の raw / `filed_date > as_of` の XBRL を読まない
- ΔIC <= 0・NOPAT <= 0・shares 欠損で `None` になり例外にならない
- applied な feature に ablation が必ず存在し、未計算は reason 付き `not_computed`
- **既定パラメータで Phase 2/3 の分布数値が変わらない**回帰
- shadow run 後に v4 `scores` の件数とフィンガープリントが不変

---

## 5. 検証手順と Phase 4 の完了条件

```powershell
# 単体
uv run pytest tests/unit/test_v5_phase4_quality.py -q
# v5 全体 + API 回帰
uv run pytest tests/unit/test_v5_skeleton.py tests/unit/test_v5_phase2.py tests/unit/test_v5_phase3_growth.py tests/unit/test_v5_phase4_quality.py -q
# 全体（現行ベースライン 883 passed を下回らないこと）
uv run pytest -q
# マイグレーション（Phase 4 が JSONB 内で完結すれば migration 不要。それが推奨）
uv run alembic current; uv run alembic upgrade head
# 実DB shadow run（universe_snapshots が存在する日を指定）
uv --cache-dir .uv-cache run python -m autoscreener.cli run-v5-shadow --date 2026-09-XX
# frontend（Phase 4 では変更不要だが壊れていないことの確認）
cd frontend; npm test; npm run build
```

**実データで必ず数えて doc に書く項目**（Phase 3 doc と同じ形式）:

- run ID / as_of / config hash / population / PIT-ready / available・unavailable 分布数 / objective 行数
- feature ごとの母集団 coverage と、runtime enabled / coverage-gated の別
- PIT違反件数（as_of より後の証拠を使った行）= 0
- 確率順序違反 = 0、quantile 順序違反 = 0、ES10 > P10 = 0
- applied なのに ablation が無い行 = 0、全銘柄が全 Phase 4 feature の ablation 枠を持つ
- v4 `scores` の行数とフィンガープリントが run 前後で不変
- API smoke（`/api/v1/models/v5/runs/latest`、`/scores?objective=...`、`/scores/{ticker}`）が 200 で `v5.phase4` を返す

**DB に対する一切の集計・測定（v4 フィンガープリントに限らない）はテストを
実行していない状態で取ること（2026-09-03 追記、2026-09-03 Phase 7 再監査で
強化）。** `tests/unit/test_api_routes.py` 等は本物の `scores` テーブルへ実際に
`Score` 行を INSERT し、テスト終了時に自分で削除する（専用のテスト用DBは無く、
`session_scope()` は `.env` の `DATABASE_URL` が指す開発DBをそのまま使う —
`tests/conftest.py` に DB分離のフィクスチャは無い）。監査中、`pytest` 実行中に
`run-v5-shadow` を重ねて実行したところ、フィンガープリント照合の途中で行数が
一時的に 8,225→8,226 に見えた（テスト終了後に 8,225 へ戻った）。

**さらに、`test_api_routes.py` は `Score` 行だけでなく `universe_snapshots` /
`raw_snapshots` にも未来日付（`_TODAY = datetime.date(2099, 1, 1)`、同ファイル
23行目のコメント「実データと衝突しない未来日付を使う」）の行を多数のフィクスチャで
INSERT する（`UniverseSnapshot(snapshot_date=_TODAY, ...)` / `RawSnapshot
(available_from=..., ...)`、同ファイル各所）。テスト実行中に `MAX(snapshot_date)`
系のクエリを打つと `2099-01-01` を拾ってしまい、実際に Phase 7 再監査でこれが
発生して監査側の集計クエリが壊れた（overlap が 0件になった）。テスト終了後は
9件 / 2026-09-02 に正しく戻る。**

**テストと shadow run・その他の DB 集計クエリを同時に走らせないこと。** 数値は
「pytest が完全に終了してから」「次の pytest を開始する前に」の静止した瞬間に
取る。テスト用DB分離（savepoint/rollback フィクスチャ等）は
docs/model_v5_phase7_backtest_infrastructure_2026-09-03.md で調査済みだが、
実装はしていない（影響範囲が大きく別途判断が必要）。

**完了条件:** 上記が揃い、`docs/model_v5_phase4_quality_<日付>.md` に「実データで実際に効いた feature」と「coverage gate で効かなかった feature」を分けて記録できていること。効かなかったことは失敗ではない（Phase 3 の TAM/KPI/guidance と同じ扱い）。

---

## 6. Phase 5 以降の下ごしらえ（着手前に読む）

- **Phase 5**: `capital_allocation_events` / `debt_instruments` / `liquidity_facilities` / `dilution_capacity`。buyback を7年複利で機械延長しない（Issue §7）。future dilution（ATM残・shelf・未行使オプション）は v4 の historical dilution と**二重計上しない**。coverage ledger の dataset 名は `capital_allocation` / `debt_profile` / `management_incentives` / `operating_kpis`（[collect_investment_intelligence.py:83-88](src/autoscreener/batch/collect_investment_intelligence.py#L83-L88)）。
- **Phase 6**: `customer_concentration` / `litigation_events` / macro regime×exposure / M&A competing risk。**macro は `fred_vintage_supported=false` のため historical backtest 禁止・forward shadow のみ**。M&A は 94/94 が `unknown` なので分類 coverage 閾値未満＝**投入しない**（`unknown` を acquisition=0 とみなすのは Issue §13 の明示的禁止事項）。
- **Phase 7**: `backtest/runner.py` を v4/v5 同一評価日で回せるよう拡張。Issue §3.3 の指標群で比較し、単一KPIで昇格しない。
- **Phase 8**: UI 未着手。`RankingPage.tsx` に model/objective selector、`TickerDetailPage.tsx` に state shift 説明（「TAM headroom: growth duration 3.8y→5.2y」形式）、`ValidationPage.tsx` に champion/challenger 比較。
- **Phase 9**: `docs/model_v5_validation.md` に `PROMOTE_V5` / `KEEP_V4` / `CONTINUE_SHADOW` の Decision Record。

---

## 7. してはいけないこと

Issue §35 の12項目に加え、このリポジトリ固有:

1. v4 `scores` テーブルへ書き込む／v5 のために v4 の挙動を変える
2. 未取得を 0 点・悪い会社として扱う（欠損は prior + confidence 低下）
3. accounting quality を rank penalty（平均引き下げ）にする — 不確実性へ効かせる
4. coverage gate を外して「行がある銘柄だけ」に feature を効かせる（coverage=優位バグ）
5. `FinancialPeriod` に無い項目を推定値で埋める
6. パラメータを config を経由せずコードへ直書きする（config hash に入らず再現不能になる）
7. `PIPELINE_STAGE_SEQUENCE` の既存番号を動かす（`model_v5_shadow`=23、`monitoring`=24、`backup`=25）
8. **09:00 JST の Windows スケジュール実行バッチを勝手に起動・停止・再実行する**（Phase 1–3 は専用 `run-v5-shadow` のみで検証している。同じ規律を守る）
9. テストだけ通して実DB coverage を確認せず「完了」と書く
10. Issue 本文と現行コードが食い違ったとき、推測で合わせる（証拠を取り、差分と理由を doc と Issue へ記録してから進む）

---

## 8. 参照インデックス

| 対象 | 場所 |
|---|---|
| Issue #3 本文 | https://github.com/oh12kj-dot/autoscreener/issues/3 |
| shadow 実行 | `uv --cache-dir .uv-cache run python -m autoscreener.cli run-v5-shadow --date YYYY-MM-DD` |
| v5 API | `/api/v1/models/v5/runs/latest`, `/api/v1/models/v5/scores`, `/api/v1/models/v5/scores/{ticker}`（[routes.py:3710](src/autoscreener/api/routes.py#L3710) 以降） |
| v5 テーブル | `model_runs` / `model_scores` / `objective_scores`（[db/models.py:245-320](src/autoscreener/db/models.py#L245-L320)） |
| Phase 3 の signal builder 雛形 | [growth.py:306-405](src/autoscreener/scoring/v5/growth.py#L306-L405) |
| Phase 3 の state 更新雛形 | [growth.py:431-506](src/autoscreener/scoring/v5/growth.py#L431-L506) |
| ablation 契約 | [engine.py:199-248](src/autoscreener/scoring/v5/engine.py#L199-L248) |
| coverage bias 監査 | `uv --cache-dir .uv-cache run python -m autoscreener.cli audit-coverage-bias` |
