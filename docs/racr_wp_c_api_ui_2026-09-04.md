# WP-C — RACR contractのAPI/UI反映 実装記録

**作業日:** 2026-09-04
**基準計画:** `docs/racr_integrated_redesign_plan_2026-09-04.md`(WP-C行)
**基準契約:** `docs/racr_wp_b_output_contract_2026-09-04.md`(v5.racr1)
**基準監査:** `autoscreener_racr_integrated_redesign_audit_2026-09-04.md` §6(出力設計)・§9(Ranking UI案)
**開始HEAD:** `14c1e54`(clean、origin/main同期済み)

---

## 0. スコープと不変条件の遵守

計画第1節の4つの不変条件に対する本WPの対応:

1. **V4を壊さない。** `scoring/moic.py` 系・v4 API・v4画面(`RankingPage`/`TickerDetailPage`のv4部分)は一切変更していない。v5関連コンポーネントは既存どおり完全に別ファイル・別JSXブロックのまま。
2. **未実装を0で埋めない。** `p_permanent_loss` / `expected_max_drawdown` / `p_mdd_above_30/50/70` / `recovery_time_median` が `null` の行は、一覧・詳細のどちらでも専用コンポーネント `V5UnavailableMetric` で「— 未推定」とだけ表示し、理由(`unavailable_reason`)を常にnative titleツールチップへ載せる。0%・空文字列としては一度も描画しない(§3・§7のテストで固定)。
3. **defaultの切替は最後。** フロントエンドのどこにも `risk_adjusted_compounding` や `"ten_bagger"` をデフォルト値としてハードコードしていない。`V5RankingSection` は `fetchV5Objectives()` が返す `default_objective` のみを初期値に使う(§4)。
4. **policy parameter(λ)をfitしない。** UIはλの値をそのまま表示するだけで、UI側で再計算・再調整する箇所は無い。

変更したファイル:

- API: `src/autoscreener/api/routes.py`(`list_v5_scores`)、`src/autoscreener/api/schemas.py`(`ModelV5ScoreListResponse`)
- テスト(新規): `tests/unit/test_v5_racr_wp_c.py`
- フロントエンド型: `frontend/src/api/types.ts`、`frontend/src/api/client.ts`
- フロントエンドラベル/整形: `frontend/src/v5Labels.ts`
- フロントエンドコンポーネント: `frontend/src/components/V5UnavailableMetric.tsx`(新規)、`V5RankingSection.tsx`、`V5TickerDetailSection.tsx`、`V5WarningBadges.tsx`
- フロントエンドページ: `frontend/src/pages/TickerDetailPage.tsx`、`frontend/src/pages/ScoreReferencePage.tsx`、`frontend/src/glossary.ts`
- フロントエンドテスト(新規): `frontend/src/components/V5RankingSection.test.tsx`
- スタイル: `frontend/src/index.css`

`RankingPage.tsx` はコード変更なし——`V5RankingSection` が同じ `useSearchParams()` (React Router)の場所を共有して自前で `objective`/`as_of` を読み書きするため、既存の `h`/`m`/`model` パラメータと衝突しない形でURL状態を持てる。

---

## 1. API: distribution契約のエンドツーエンド到達確認(B-4の検証)

`_v5_distribution_payload()`(`routes.py`)は `dict(score.distribution)` をそのまま展開するだけで、WP-Bが追加した新フィールドに一切手を触れていないことをコードで確認済み(WP-Bの記録どおり)。本WPでは**実際にHTTPレスポンスに出るところまで**をテストで固定した:

`tests/unit/test_v5_racr_wp_c.py::test_racr_distribution_fields_reach_the_api_response` が:

- `ce_cagr` / `p_cagr_above_15/20/25` が算出済み distribution で non-null であること
- `p_permanent_loss` / `expected_max_drawdown` / `p_mdd_above_30/50/70` / `recovery_time_median` が **常に `null`** で、対応する `*_unavailable_reason` が**常に非空文字列**であること

をAPIレスポンスJSONに対して直接検証する。

`test_racr_objective_score_reports_omitted_terms_through_the_detail_api` は、詳細エンドポイント(`GET /models/v5/scores/{ticker}`)経由でも `explanation.omitted_terms == ["drawdown", "permanent_loss"]` が読めることを確認する。

---

## 2. API: ランキングfilter(監査§5.4/§9.1)

`GET /api/v1/models/v5/scores` に3つのfilterを追加した(既存パラメータ・既存レスポンスフィールドは1つも変更していない):

| パラメータ | 対象 | 備考 |
|---|---|---|
| `min_confidence` | `ModelScore.confidence` | 0.0〜1.0 |
| `sector` | `Ticker.sector` | 完全一致(v4の `GET /candidates` と同じ規約) |
| `min_p_cagr_above_20` | `distribution.p_cagr_above_20` | 0.0〜1.0 |

**意図的に実装しなかったfilter:** 「永久損失の上限」「P(MDD>50%)の上限」。理由は計画の不変条件2そのもの——これらのフィールドは現行モデルで常に `null` であり、閾値フィルタを掛けると常に全件が除外される(=「該当なし」の意味を持たない誤動作)。`list_v5_scores` のdocstringにこの理由を明記し、`test_no_filter_over_unavailable_metrics_is_accepted` でAPIの関数シグネチャに `max_permanent_loss`/`max_p_mdd_above_50` が存在しないことを固定した。フロントエンドはこれに対応する入力欄を**表示はするが無効化し、理由をtitleで示す**(§5参照)——「そもそも選べない」より「なぜ選べないか」が伝わるほうが誤解が少ないという判断。

フィルタ実装はPython側のフィルタリング(全件取得 → filter → slice)にした。理由: 対象フィールドの一部(`p_cagr_above_20`)が `ModelScore.distribution` のJSONB内にあり、SQL側のJSONPath式より、1 run分(実測1,266銘柄)を素通りするPythonループのほうが単純で壊れにくいと判断したため。既存の `rank asc nullslast, symbol asc` という並び順は変えていない。

---

## 3. API: 「このrunはこのobjectiveを計算していない」を明示するフィールド

`ModelV5ScoreListResponse` に `objective_computed_for_run: bool`(デフォルト`True`、後方互換用)を追加した。`list_v5_scores` は、選択objectiveの `ObjectiveScore` 行が対象runに1件でも存在するかを、filter適用前に別途1クエリで確認してこの値に入れる。

これにより、一覧が空(`items == []`)になる3通りの原因を区別できる:

| ケース | `objective_computed_for_run` | `total` |
|---|---|---|
| ①このrunはこのobjectiveのcontract追加前に実行された | `False` | `0` |
| ②objectiveは計算済みだがfilter/母集団の結果ゼロ件 | `True` | `0` |
| ③objectiveは計算済みで実際に該当あり | `True` | `>0` |

①と②は将来別々のUI文言にできるようフィールドを分けたが、現時点のUIでは①だけを専用の強調メッセージにし、②③は同じ「該当する候補がありません」で構わないとした(②③はどちらも「候補が無い」という意味で正しく、①だけが「objectiveそのものが無い」という別の意味を持つため)。

テスト3本(`test_objective_computed_for_run_*`)でこの3ケースを固定した。①の再現には「`ten_bagger` だけを持つ、RACR契約追加前を模したrun」フィクスチャ(`pre_racr_run`)を使っている——2026-09-04より前に書かれた実runと同じ形。

---

## 4. フロントエンド: 型・default_objectiveの扱い

`frontend/src/api/types.ts` の `ModelV5Distribution` にWP-Bの全フィールドを追加し(nullable、`_unavailable_reason`付き)、`ModelV5ScoreListResponse` に `objective_computed_for_run: boolean` を追加した。

`V5RankingSection` の objective 初期値は次の1箇所だけで決まる:

```ts
fetchV5Objectives().then((res) => {
  setObjectivesData(res);
  setObjectiveState((current) => current || res.default_objective);
});
```

`risk_adjusted_compounding` という文字列はラベル辞書(`v5Labels.ts`)以外のどこにも書いていない——選択肢としては出るが、初期選択・default値としては一切書いていないことをコードで確認できる(grep `risk_adjusted_compounding` の一致箇所はラベル定義とテストのみ)。

---

## 5. フロントエンド: Ranking一覧(監査§9.1準拠)

`V5RankingSection.tsx` の列構成(左から):

順位 / 銘柄 / **選択中objectiveの値**(既存) / **CE CAGR**(新規) / 期待CAGR(既存) / **中央値CAGR**(新規) / **P(15/20/25%)**(新規、3値を1セルに縦積み) / **上方余地 P(10x)**(旧「P(10x)」列を改称・移設。削除はしていない) / **永久損失**(新規) / **予想MDD**(新規) / **信頼度・鮮度**(既存の信頼度列を拡張) / warnings

永久損失・予想MDD列は、値が `null` の間は必ず `<V5UnavailableMetric reason={...} />` を描画する(「— 未推定」+ native titleツールチップ)。値が入るようになった場合(将来WP-Fで実装後)は自動的に確率表示へ切り替わる分岐をあらかじめ入れてある。

信頼度・鮮度列は `confidence` に加えて、その行の `warnings` 配列から `raw_snapshot_not_available_as_of` / `financial_statement_pit_is_approximate` の有無を見て「スナップショットなし」「PIT近似」「良好」を出す——新しいデータを取得せず、既存の行単位warningsから素直に導出している。

フィルタ行(`.filters`)に5つ追加した: 信頼度下限・セクター・P(CAGR>20%)下限(実際にAPIへ送る)、永久損失上限・P(MDD>50%)上限(`disabled`、titleに理由)。

3つの空状態:

1. `!data.objective_computed_for_run` → `.v5-objective-not-computed` の専用ブロック(「このrunはまだ計算していません」)。
2. `objective_computed_for_run && items.length === 0`(filter適用時) → 既存の「該当する候補がありません」に「(フィルタ条件を満たす銘柄がありませんでした)」を付記。
3. 同上、filter未適用 → 既存の「該当する候補がありません」のみ。

objectiveのURL永続化は `V5RankingSection` 自身の `useSearchParams()` で行う(`?objective=risk_adjusted_compounding`)。`as_of` も同じ仕組みで対応済みだが、現時点でas_ofを変更するUIコントロールは無い(既定=最新run)ため、手動でURLに付けられた場合にのみ効く形——「常に最新runを見る」という既存UXを崩さないための意図的な非対称。一覧の各行の銘柄リンクは `?model=v5&objective=...&as_of=...`(as_ofが有効な場合のみ)を付けて詳細ページへ渡す。

---

## 6. フロントエンド: Ticker Detail(監査§9.2準拠)

`V5TickerDetailSection.tsx`:

- `objective` propを新設。URL(`TickerDetailPage`経由)から渡された値を優先し、無指定ならAPIの`default_objective`。
- `detail.objectives` に選択objectiveが**見当たらない**場合(=このrunはこのobjectiveを計算していない)を、ランキングと同じ文面の専用ブロックで明示する——「未計算」の1語だけで済ませていた旧実装から変更。
- 「分布の主要な数値」テーブルを新設: CE CAGR、期待/中央値CAGR、P(CAGR>15/20/25%)、大幅元本毀損確率(旧`p_moic_below_0_5`の改称値)、下位10%期待損失、永久損失、予想MDD、P(MDD>30/50/70%)、回復期間中央値。後半4項目は現行モデルでは必ず`V5UnavailableMetric`経由になる。
- 選択objectiveがRACR (`risk_adjusted_compounding`) かつ `status === "available"` のとき、「RACRの内訳」テーブルを表示: CE CAGR・TailLoss10・DDExcess・P(PermanentLoss)・ModelUncertaintyの各値と対応するλ、および `omitted_terms`(常に「ドローダウン・永久損失」の日本語表記)・`ce_cagr_failure_floor`。RACRが選択されていない場合はこの節ごと非表示——他objectiveの画面にRACR固有の内訳を混ぜない。
- 末尾の確認行に `run.as_of` / `distribution.status` / `contract_version` / target horizon・倍率を追記し、「どのrunのどの分布を見ているか」を常設で分かるようにした(監査§9.2「実際に使った値・日付」の一部)。

---

## 7. フロントエンド: warningの分類(V5WarningBadges)

`v5Labels.ts` に `v5WarningCategory()` を追加し、コードを4分類(`stale` / `unsupported` / `low_coverage` / `unvalidated` / `other`)へ分けた。`V5WarningBadges` が `warning-tag--<category>` クラスを付け、`index.css` で色分けした(鮮度=既存amber、構造的未対応/その他=neutral、coverage不足=accent、未検証=danger)。ラベル文言・description文言は変更していない(既存の `v5WarningLabel`/`v5WarningDescription` をそのまま使用)。

---

## 8. 用語集・スコア説明ページ

`glossary.ts` に新カテゴリ `v5racr`(「v5・RACR(実験段階)」)を追加し、5用語(`ce-cagr` / `racr` / `expected-shortfall-log` / `permanent-loss` / `max-drawdown`)を数式抜きの日本語で追加した。特に `permanent-loss` と `max-drawdown` の本文は、「— 未推定」の意味を明文化している(「0%という意味では絶対にない」)。

`ScoreReferencePage.tsx` の末尾に「v5(実験段階)の RACR について」節を追加した。既存のv4説明(このページの大半)は一切変更していない。RACRの4項目の式・`omitted_terms`の意味・未推定表示の意味を平易な日本語で説明する。

---

## 9. 数値の表示形式(監査§6.3)

`v5Labels.ts` に `v5FormatRate()`(CAGR/RACR系、常に0.1pt=小数1桁)と `v5FormatProbability()`(確率系、1%以上は0.1pt、1%未満は既存v5画面の慣例を踏襲して0.01ptまで——P(10x)のように中央値が0.1%未満の指標を1桁に丸めると銘柄間の差が消えるため)を追加し、新規に描画するすべてのRACR関連数値をこの2関数経由にした。内部のPython floatをテンプレートへ直接埋め込んでいる箇所は無い(既存の`toFixed`呼び出しパターンを踏襲)。

---

## 10. 実行したテストとその結果(実測)

### バックエンド

```
$ TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
  uv run pytest tests/unit/test_v5_racr_wp_c.py -q
9 passed in 1.42s
```

```
$ TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
  uv run pytest tests/ -q
1076 passed in 29.95s
```

タスク開始時点の基準(1067 passed, 0 failed)から **+9(新規WP-Cテスト)、失敗0件**。既存テストへのリグレッションは無い。

### フロントエンド

```
$ npm run build
tsc -b && vite build
✓ built in 2.33s(型エラー無し)
```

```
$ npm test -- --run
Test Files  2 passed (2)
     Tests  4 passed (4)
```

既存2件(`InvestmentIntelligenceSections.test.tsx`)+ 新規2件(`V5RankingSection.test.tsx`: 「永久損失/MDDが0%として描画されないこと」「run未計算時に専用メッセージが出ること」)。

```
$ npm run lint
oxlint: 17 warnings, 0 errors
```

タスク開始時点の基準(既存~17件)と同数。全て `react(set-state-in-effect)` / `react(only-export-components)` という、このWP以前から存在するパターン由来の警告で、新規に増えたものは無い(件数を実測比較して確認済み)。

---

## 11. 未実施・持ち越し事項

1. **実ブラウザでの見た目確認は行っていない。** 本WPの検証は `npm run build` / `npm test` / `npm run lint` と、テスト内のjsdomレンダリングのみ。実際にブラウザでランキング画面・詳細画面を開いての目視確認はしていない。
2. **WP-Bで持ち越されたrun-metrics診断(RACR vs expected_returnのSpearman・Top20重複)は本WPでも未実装のまま。** `engine.py` の変更が必要で、WP-CのAPI/UIスコープの外。なお、2026-09-04の実runに対する手計算診断が既に `docs/racr_shadow_run_diagnostic_2026-09-04.md` として別途存在し、そこでは**現行RACRがCE CAGRのアフィン変換に退化している**(Spearman 1.0000000000)ことが判明している——UI側はこの数値をそのまま正直に表示しているだけで、この退化自体はモデル層(WP-B/将来WP)の課題であり、本WPでは修正していない。
3. **市場規模(時価総額)・データ鮮度・coverageによるfilterは未実装。** 監査§9.1が列挙する項目のうち、`Ticker`/`ModelScore`から素直に取れる3つ(信頼度・セクター・P(CAGR>20%))のみを実装した。時価総額はv5の`ModelScore`/`Ticker`に直接持たせておらず、別テーブルへのjoinが追加で必要になるため、本WPでは見送った。
4. **as_ofを選択するUIコントロールは追加していない。** URLパラメータとしては透過するようにしたが(共有リンクの再現性のため)、UI上で日付を選ぶ手段はまだ無い。
