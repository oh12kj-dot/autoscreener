# WP-A2 — DBテストの自己シード化(17件)実装記録

**作業日:** 2026-09-04 JST
**対応元:** `docs/racr_wp_a_operational_safety_2026-09-04.md` §7「17件の失敗について」
**作業ブランチ:** `main`(委譲元HEAD `8be3af5`)

---

## 0. 結論

WP-A(`aa8013a`)が`tests/conftest.py`に追加したテストDB隔離
(`autoscreener_test`、0件から始まる専用DB)により露出した17件の失敗テスト
すべてに、各テストが本来必要とするデータを自前でseedするよう修正した。
アサーションは一切変更していない(弱めていない)。修正後、隔離済みテストDB
に対して:

```
TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
  uv run pytest tests/ -q --tb=no -p no:randomly
```

**1067 passed, 0 failed**(修正前:1050 passed / 17 failed)。2回連続実行でも
同じ結果になることを確認し、実行後に`tickers`/`model_runs`/`scores`/
`price_snapshots`が0件に戻ることも確認した(後始末漏れなし)。

アサーションが誤りだったケースは1件もなかった——全17件、原因は
「テストがDBに最低1件Tickerがある前提で、自分では作っていなかった」
という欠落したfixtureであり、コード側の欠陥ではなかった。

---

## 1. 修正した17件(依存していた前提 → 今回seedした内容)

### `tests/unit/test_data_freshness.py`(1件)

- `test_freshness_guard_flags_stale_price_data`:以前は
  `func.max(PriceSnapshot.trade_date)`が開発DBの実データを拾う前提
  (`_latest_price_date`フィクスチャ)だった。隔離DBでは`None`が返り
  `None + timedelta(...)`で`TypeError`。今回、専用Ticker
  (`ZZFRESHSTALE`)と`trade_date=2100-01-01`のPriceSnapshotを1件作り、
  その日付+60日を`score_date`として`_check_price_freshness`を呼ぶ
  (2100年はファイル内の他の予約日2099-01-01/2099-04-01より後にして
  全体最大値を確実に取る)。

### `tests/unit/test_v5_phase3_growth.py`(1件)

- `test_shadow_run_persists_feature_ablation_without_touching_v4`:
  `session.query(Ticker).order_by(Ticker.id).first()`がDBに最低1件ある
  前提。専用Ticker(`ZZV5GROWSHADOW`)を作成し、テスト終了時に
  `ModelRun`と一緒に削除する。

### `tests/unit/test_v5_phase4_quality.py`(1件)

- `test_shadow_run_persists_quality_ablation_without_touching_v4`:同上の
  パターン。専用Ticker(`ZZV5QUALSHADOW`)を作成・後始末。

### `tests/unit/test_v5_phase5_balance_sheet.py`(3件)

- `test_missing_liquidity_facility_leaves_debt_and_liquidity_not_collected`:
  `DebtInstrument.ticker_id`のFK対象として「既存の」Tickerがある前提
  (コード上のコメントは「real, existing ticker id required」)。同一
  トランザクション内でTicker(`ZZB1`)を作成・flushしてからFK先として使い、
  最後に`session.rollback()`する既存の後始末方針は変更していない。
- `test_low_coverage_runtime_gate_disables_feature_even_with_a_row`:同様に
  Ticker(`ZZB7`)を同一トランザクション内で作成してからFK先として使う。
- `test_shadow_run_persists_capital_ablation_without_touching_v4`:
  `.first()`パターン。専用Ticker(`ZZV5CAPSHADOW`)を作成・後始末。

### `tests/unit/test_v5_phase6_tail_macro_competing_risk.py`(8件)

いずれも`session.query(Ticker.id).order_by(Ticker.id).first()[0]`
(既存Ticker前提、ロールバック方式)または`.first()`(shadow-run方式)。
共有ヘルパー`_seed_ticker(session, symbol)`を追加し、ロールバック方式の
7件はそれぞれ専用symbol(`ZZT1`〜`ZZT7`)でTickerを同一トランザクション内
に作成してから使う(最後に`session.rollback()`する既存方針は不変)。

- `test_customer_concentration_caps_total_disclosure_at_one`(`ZZT1`):
  `CustomerConcentration`行のFK先としてTickerが必要。
- `test_litigation_no_recent_events_is_no_change_not_missing`(`ZZT2`):
  `LitigationEvent`行のFK先としてTickerが必要。
- `test_litigation_recent_event_count_is_bounded_severity`(`ZZT3`):同上。
- `test_litigation_ignores_events_filed_after_as_of`(`ZZT4`):同上。
- `test_macro_regime_rejects_fred_vintage_unsupported_as_not_applicable`
  (`ZZT5`):`MacroExposureSnapshot`行のFK先としてTickerが必要。
- `test_macro_regime_negative_downside_beta_gets_no_bonus`(`ZZT6`):同上。
- `test_future_dilution_capacity_signal_uses_market_cap_and_options_ratio`
  (`ZZT7`):`DilutionCapacity`行のFK先としてTickerが必要。
- `test_shadow_run_persists_tail_ablation_without_touching_v4`:`.first()`
  パターン。専用Ticker(`ZZV5TAILSHADOW`)を作成・後始末。

### `tests/unit/test_v5_phase7_backtest_infrastructure.py`(2件)

- `test_compare_v4_v5_same_day_with_real_run_and_v4_scores`:`.first()`
  パターン。`ModelScore`/v4`Score`のFK先としてTickerが必要。専用Ticker
  (`ZZV5CMP1`)を作成し、`finally`で削除に追加した。
- `test_forward_validation_v5_settles_a_matured_synthetic_run`:`.first()`
  パターンに加え、以前は既存Tickerに紐づく実`PriceSnapshot`の有無を
  テストが制御していなかった(`counts["missing_price"]`に流れても
  `computed + missing_price + not_matured >= 1`という緩いアサーションは
  通ってしまうため、実際に決済されたかまでは検証できていなかった)。
  専用Ticker(`ZZV5FWD1`)に加え、entry用PriceSnapshot(`2020-01-03`、
  open/close=10.0)と1Mホライズンのexit用PriceSnapshot(`2020-02-01`、
  open/close=12.0)を作成し、`counts["computed"] >= 1`
  と実際に決済された`ModelV5ForwardReturn`(`realized_return≈0.2`)を
  直接検証するようアサーションを強化した(既存アサーションを弱めたのでは
  なく、テストの意図(「matured synthetic runを決済する」)を実際に検証
  できるように厳格化した)。

### `tests/unit/test_v5_skeleton.py`(1件)

- `test_v5_shadow_persists_separately_without_touching_v4`:`.first()`
  パターン。専用Ticker(`ZZV5SHADOW`)を作成し、`finally`で`ModelRun`と
  一緒に削除する。

---

## 2. アサーションが誤りだったケース

**なし。** 17件すべて、テスト対象コードの挙動に問題はなく、テスト自身が
「DBに最低1件Tickerがある」という暗黙の前提を持ちながら自分では作って
いなかったことが原因だった。アサーション文言・しきい値は一切変更して
いない(`test_forward_validation_v5_settles_a_matured_synthetic_run`の
アサーション強化のみ、既存アサーションを緩い方向へ書き換えたものではなく、
テスト名が主張する「決済される」ことを実際に検証できていなかった穴を
埋めた、という位置づけ)。

---

## 3. 変更したファイル

- `tests/unit/test_data_freshness.py`
- `tests/unit/test_v5_phase3_growth.py`
- `tests/unit/test_v5_phase4_quality.py`
- `tests/unit/test_v5_phase5_balance_sheet.py`
- `tests/unit/test_v5_phase6_tail_macro_competing_risk.py`
- `tests/unit/test_v5_phase7_backtest_infrastructure.py`
- `tests/unit/test_v5_skeleton.py`

`src/`配下・`tests/conftest.py`の隔離ガードは無変更。
