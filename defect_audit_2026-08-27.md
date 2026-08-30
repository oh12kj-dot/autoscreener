# 第三者監査:欠陥一覧と修正指示(2026-08-27)

## この文書について

`defect_fixes_2026-08-26.md`(D-1〜D-10)と `model_audit_v4_2026-08-26.md`
(S-1〜S-9・A-1〜A-2・B-1〜B-8・C-1〜C-7)は、いずれも**同じ担当者が書いて
同じ担当者が検証した**監査記録である。本書はそれとは独立に、コードを
1行ずつ読み直して行った第三者監査である。

## 監査方法

1. まず既存2文書に記録された修正(D-1・D-4・D-7・D-8・D-9・D-10・S-1・S-2・
   S-5・S-6・S-7・S-8・A-1・B-5・B-6・B-8・C-1・C-4)が、**文書の主張どおり
   実際のコードに反映されているか**を、行番号を挙げて1件ずつ照合した。
2. その上で、**両文書に記載のない新規の欠陥**を、4つの独立した観点
   (①価格・通貨・ポイントインタイムのデータ構築、②バックテスト・統計・
   較正、③データ収集・除外ゲート・日次バッチ、④API・フロントエンド表示)
   から separately に調査した。
3. 各観点の指摘は、この文書の作成者が該当ファイルを直接読み直して裏取り
   した(行番号・コード引用は本書作成者が実物のファイルから確認済み)。

## 既存修正の照合結果:**全項目、記録どおりコードに反映されていた**

D-1・D-4・D-7・D-8・D-9・D-10・S-1・S-2・S-5(段階1)・S-6(段階1)・S-7・
S-8(監視のみ)・A-1・B-5・B-6・B-8・C-1(バックエンド)・C-4 は、いずれも
実装・設定・テストが揃って配線されていることを確認した。**過去の教訓
(「宣言されているが配線されていない」)は今回は再発していなかった。**

一方で、以下の**新規の欠陥9件**を発見した。うち2件は「A-1と全く同型の
バグ(欠損を有利な値に読み替える)」であり、修正が横展開されていなかった
箇所である。

---

## 欠陥一覧

| # | 欠陥 | 深刻度 | 種別 |
|---|---|---|---|
| [E-1](#e-1) | `net_debt` の構成要素(Total Debt / Cash)の欠損が「ゼロ」として扱われる | **高** | ロジック(A-1と同型) |
| [E-2](#e-2) | 収集健全性の成功率計算が `sanitized` を失敗扱いし、慢性的な誤警報を出す | **高** | 運用監視 |
| [E-3](#e-3) | `unhandled_error` がサーキットブレーカーの失敗率計算から漏れる | 中 | 運用監視 |
| [E-4](#e-4) | κ(マルチプルの成長弾力性)推定が、モデル自身がクランプ済みの成長率を説明変数に使っている | 中 | 統計・構造パラメータ |
| [E-5](#e-5) | ランキング一覧に下振れ確率が表示されない(詳細ページのみ) | 中 | 表示ロジック |
| [E-6](#e-6) | `/universe/status`(収集進捗)がフロントエンドから一度も呼ばれていない | 中 | 表示ロジック |
| [E-7](#e-7) | DBバックアップに整合性検証・リストア手順が無い | 中 | 運用・データ保全 |
| [E-8](#e-8) | デシル分割の同値(タイ)処理が非決定的な挿入順に依存する | 低 | 統計 |
| [E-9](#e-9) | 下振れ確率に用語集エントリ・ツールチップが無い | 低 | 表示 |

---

<a id="e-1"></a>
## E-1【高】`net_debt` の構成要素(Total Debt / Cash)の欠損が「ゼロ」として扱われる

### 何が起きているか

`src/autoscreener/scoring/point_in_time.py:387-389`

```python
total_debt = _latest(balance_sheet, "Total Debt") or 0.0
cash = _cash_balance(balance_sheet) or 0.0
net_debt = (total_debt - cash) * fx
```

`_latest` は該当する行(`Total Debt`)が貸借対照表ペイロードに**存在しない**
場合に `None` を返す(`point_in_time.py:177-179`)。`or 0.0` により、この
「測れない」が無条件に「有利子負債ゼロ」へ読み替えられる。

これは **A-1(希薄化の欠損を「希薄化ゼロ」として扱っていた欠陥)と全く同じ
形のバグ**であり、A-1の修正(断面中央値へのフォールバック)がこの箇所には
横展開されていなかった。同じファイル内の他の欠損値——
`equity_to_assets`(L426-429)・`fcf_margin`(L432)・
`cash_runway_quarters_annual`(L314-315)——はすべて欠損を正しく `None` の
まま伝播させており、この2行だけが扱いが異なる。

### 影響の方向性(2つあり、非対称)

- **`total_debt` が欠損 → 0円**:`net_debt` が過小(過大なネットキャッシュ)
  になる。`enterprise_value` が縮み、`current_ev_to_gross_profit` が下がり、
  `leverage_effect`・σ も小さくなる。**「データが取れなかった銘柄」が
  「無借金の優良企業」としてランキング上位に浮上しうる**方向であり、
  A-1・D-1・D-9と同種の「欠損が満点に読み替えられる」パターン。
- **`cash` が欠損 → 0円**:`net_debt` が過大になり、不当に不利化される
  方向。ランキングを歪める危険度は前者より低いが、正しい候補を
  取りこぼす可能性がある。

### なぜ単純に「欠損なら測れない扱いにする」で済まないか

`Total Debt` は yfinance の集計行であり、**本当に無借金の企業では行自体が
現れない**(0という値ではなく、キーが存在しない)可能性がある。つまり
「欠損」と「真にゼロ」を、現在のpayload構造だけからは区別できない。
D-9・D-10・S-2がそうだったように、**閾値や扱いを決める前に実データでの
発生頻度を確認する必要がある。**

### 修正指示(Sonnetへ)

**ステップ1:実データ調査(コミット前に必ず実行)**

```sql
-- ランキング可能な銘柄(gross_profit_latestが取れる)のうち、
-- Total Debt / Cash 関連行が balance_sheet ペイロードに全く無い件数を数える
WITH latest AS (
  SELECT r.ticker_id, r.payload
  FROM raw_snapshots r
  WHERE r.snapshot_date = (SELECT max(snapshot_date) FROM raw_snapshots)
)
SELECT
  count(*) FILTER (WHERE payload->'balance_sheet' -> 'Total Debt' IS NULL) AS missing_total_debt,
  count(*) FILTER (WHERE payload->'balance_sheet' -> 'Total Debt' IS NULL
                    AND payload->'balance_sheet' -> 'Long Term Debt' IS NULL
                    AND payload->'balance_sheet' -> 'Current Debt' IS NULL) AS missing_all_debt_rows,
  count(*) AS total
FROM latest;
```

(実際のJSON構造は `raw_snapshots.payload` を1件 `jsonb_pretty` で確認して
キー名を合わせること。年次系列の格納形式は `parse_period_series` の入力
仕様を `point_in_time.py` の先頭付近で確認する。)

**ステップ2:件数に応じて方針を決める**

- 発生率が小さい(例:数%未満)場合 → **`None` を返して「測れない」扱い**
  にするのが最も安全(D-1のfx換算失敗と同じ扱い)。
- 発生率が無視できない場合 → A-1と同じ**断面中央値フォールバック**方式を
  検討する(ただし `net_debt` は符号を持つ量であり中央値が意味を持つかは
  要検証。単純に「発生率が高いなら『無借金』が本当に多い母集団かもしれない」
  という可能性も排除しないこと)。

**ステップ3:最小安全策としての実装(発生率調査の結果を待たずに今すぐ入れられる)**

`MoicInputs` に診断フラグを1つ追加し、**計算方法は変えずに**まず可視化する
(S-5段階1と同じ手順):

```python
# point_in_time.py
total_debt_raw = _latest(balance_sheet, "Total Debt")
cash_raw = _cash_balance(balance_sheet)
net_debt_component_missing = total_debt_raw is None or cash_raw is None
total_debt = total_debt_raw or 0.0
cash = cash_raw or 0.0
net_debt = (total_debt - cash) * fx
```

`MoicInputs` に `net_debt_data_missing: bool = False` を追加し、
`MoicResult` → `engine.result_to_factors` → API の警告バッジ経路
(`C-4` と同じ仕組み)まで通す。**その上で、ステップ1の調査結果を見て
ステップ2の恒久修正(None化 or 中央値フォールバック)を選ぶこと。**
挙動を変える前に必ず `run-backtest` でKPIを確認する(このプロジェクトの
すべての先例が要求している手順)。

### テスト

```python
# tests/unit/test_point_in_time.py
def test_missing_total_debt_does_not_default_to_zero_silently():
    """E-1: Total Debtが欠損している銘柄で、net_debtが無借金として
    誤って有利に計算されないこと(A-1と同型の欠陥への回帰テスト)。"""
    payload = _payload_without_balance_sheet_row("Total Debt")
    result = build_moic_inputs(payload, shares, prices, as_of, sector=None)
    # 恒久修正の方針(None化 or 中央値)に応じてアサーションを調整する
    assert result is None or result.net_debt_data_missing is True
```

### 受け入れ基準

- [ ] ステップ1のSQL調査結果が本節に記録されている
- [ ] 採用した方針(None化 / 中央値フォールバック / 診断フラグのみ)が記録されている
- [ ] 回帰テストが追加され通る
- [ ] `run-backtest` のKPIが既存値から悪化していない
- [ ] 診断フラグを追加した場合、UIの警告バッジに配線されている

---

<a id="e-2"></a>
## E-2【高】収集健全性の成功率計算が `sanitized` を失敗扱いし、慢性的な誤警報を出す

### 何が起きているか

`src/autoscreener/monitoring.py:24-25`

```python
success = status_counts.get("success", 0)
success_rate = success / total
```

`snapshot_collector.py:430` は、バリデーションで一部フィールドを無効化
した上で採用したデータを `"sanitized"` というステータスで記録する
(B-7でラベルを `invalid_data` から改名したもの。**除外ではなく、
スコアリングに正常に使われる**)。ところが `check_collection_health` は
分子(`success`)にこの `sanitized` を含めない。実データでは
`sanitized` が全体の約18.7%を占めるため、平常運転でも
`success_rate` は81%程度に恒常的に張り付き、
`COLLECTION_SUCCESS_ERROR_THRESHOLD = 0.90` を毎日下回って
`logger.error` が発火し続ける。

### なぜ「根本的」か

18.7の運用アラートは「本当に異常が起きた日」を検知するための仕組みだが、
**平常時に鳴り続けるアラートは、運用者の注意をすり減らし、本物の異常
(実際に成功率が急落した日)を「またいつものエラーか」で見逃す土台を作る**
(アラート疲れ)。これは D-8(急変検知が呼ばれていなかった)の裏返しで、
「仕組みはあるが、閾値が実際のデータ分布とかみ合っていないため機能して
いない」という同系統の欠陥である。`tests/unit/test_monitoring.py` は
`sanitized` を含む入力を一度もテストしていない。

### 修正指示(Sonnetへ)

```python
# monitoring.py
def check_collection_health(status_counts: dict[str, int]) -> None:
    total = sum(status_counts.values())
    if total == 0:
        return
    # E-2: "sanitized" は一部フィールドを無効化した上でスコアリングに
    # 正常採用されたデータであり、収集の失敗ではない(B-7)。除外扱いの
    # 状態(permanent_failure等)とだけ区別すればよい。
    success = status_counts.get("success", 0) + status_counts.get("sanitized", 0)
    success_rate = success / total
    ...
```

`sanitized` の比率自体は別途監視する価値があるので、削除するのではなく
**別のログとして残す**:

```python
    sanitized_ratio = status_counts.get("sanitized", 0) / total
    if sanitized_ratio > 0.30:  # 閾値は実データ分布を見て調整
        logger.warning(
            "sanitized data ratio elevated: %.1f%% (%d/%d) — data quality may be degrading",
            sanitized_ratio * 100, status_counts.get("sanitized", 0), total,
        )
```

(この追加の閾値0.30は仮値。実データの `sanitized` 比率の分布を
`collection_logs` から確認し、根拠を持って決めること。)

### テスト

```python
# tests/unit/test_monitoring.py
def test_sanitized_status_counts_toward_success_rate():
    """E-2: sanitizedは失敗ではなく正常採用データなので、成功率の分子に含まれること。
    実データでの発生率(約18.7%)相当の入力でERRORが発火しないことを確認する。"""
    status_counts = {"success": 700, "sanitized": 200, "permanent_failure": 20}
    # 700+200=900 / 920 ≈ 97.8% となり、閾値0.90を上回ってerrorが出ないこと
    check_collection_health(status_counts)  # caplogでERRORが出ないことを確認
```

### 受け入れ基準

- [ ] `success_rate` の分子に `sanitized` が含まれる
- [ ] `sanitized` 比率自体の監視が別途追加されている(閾値は実データ根拠つき)
- [ ] 上記テストが通る
- [ ] 過去ログ(`collection_logs`)を使って、修正後の閾値が平常運転日に誤発火しないことを確認する

---

<a id="e-3"></a>
## E-3【中】`unhandled_error` がサーキットブレーカーの失敗率計算から漏れる

### 何が起きているか

`src/autoscreener/batch/parallel_runner.py:30-35`

```python
DEFAULT_FAILURE_STATUSES = {
    "transient_failure",
    "empty_response",
    "empty_response_delisted",
    "parse_failure",
}
```

`worker_fn` が分類外の例外を投げると `status = "unhandled_error"`
(L90-91)として記録されるが、この集合に含まれないため
`failures`(L96)にカウントされない。コード側のバグ(DB接続断・
NULL参照等)で全銘柄が例外を送出しても、サーキットブレーカーの
失敗率は0%のまま推移し、18.4が意図する「広域異常時の早期停止」が
作動しない。ログ(`logger.exception`)には残るため完全な握りつぶし
ではないが、**運用者がログを見るまで、実際には全滅している実行が
「進行中」のまま最後まで走り続ける**。

### 修正指示(Sonnetへ)

```python
# parallel_runner.py
DEFAULT_FAILURE_STATUSES = {
    "transient_failure",
    "empty_response",
    "empty_response_delisted",
    "parse_failure",
    "unhandled_error",  # E-3: 分類外の例外もサーキットブレーカーの対象にする。
    # worker_fn側のバグ(DB接続断等)による広域失敗を検知できないままにする
    # ことこそ避けるべきで、permanent_failure(想定内の除外)とは性質が違う。
}
```

### テスト

```python
# tests/unit/test_parallel_runner.py
def test_unhandled_error_counts_toward_circuit_breaker():
    """E-3: worker_fnが分類外の例外を投げ続けた場合、サーキットブレーカーが作動すること。"""
    def always_raises(symbol, run_id):
        raise RuntimeError("boom")
    status_counts = run_parallel(symbols=[...many symbols...], worker_fn=always_raises, ...)
    # circuit_breaker_tripped=True がログ or 戻り値経由で確認できること
```

### 受け入れ基準

- [ ] `unhandled_error` が `DEFAULT_FAILURE_STATUSES` に含まれる
- [ ] 上記テストが通る
- [ ] 既存の日次収集での `permanent_failure` 除外の意図(想定内の恒久除外は
      ブレーカーの対象外)が壊れていないことを確認する

---

<a id="e-4"></a>
## E-4【中】κ(マルチプルの成長弾力性)推定が、クランプ済みの成長率を説明変数に使っている

### 何が起きているか

`config/scoring.yaml` のコメントは κ について「**リターンには一切
フィットさせていない、断面の値づけ構造そのもの**」「これは較正ではなく
測定である」と強調している。しかし `src/autoscreener/backtest/runner.py`
の `estimate_elasticity_over_history` が回帰の説明変数として使う
`growth` は `scoring/moic.py:base_initial_growth` の戻り値であり、これは

```python
return _clamp(min(candidates), growth.min_initial_rate, initial_growth_ceiling(inputs, config))
```

によって `max_initial_rate`(0.60)または
`max_initial_rate_single_observation`(0.45)に**既にクランプ済み**の値
である。model_audit文書のS-6では「上位30の17%が成長率上限に張り付く」
と確認されており、高成長企業ほどこのクランプに当たりやすい。

### 何が問題か

クランプされた銘柄群は、真の成長率が(例えば)80%の企業も150%の企業も
すべて同じ点(0.60)に潰れて回帰に入る。これは統計学でいう**打ち切り
(censored)回帰**の状態であり、高成長域でX軸の分散が人為的に圧縮される
ため、推定される傾き κ は**減衰方向にバイアスがかかりうる**
(regression dilution)。「観測可能な事実を測定しているだけ」という
docstringの主張と、実際にはモデル自身の丸めを経由した値を説明変数に
使っている実装との間に、看過できない食い違いがある。

**ここでの結論は「κを直ちに変えるべき」ではない**——影響の大きさは
未検証であり、`config/scoring.yaml` 冒頭が要求する「リターンにフィット
させて手で動かさない」という規律をこちらも守るべきだからである。まず
測定方法自体の妥当性を検証することが先。

### 修正指示(Sonnetへ)

**変更ではなく、まず検証を追加する。**

1. `estimate_elasticity_over_history` に、クランプされていない銘柄
   (`raw = min(candidates)` が `initial_growth_ceiling` 未満)だけに
   限定した場合のκを別途算出し、両方をログ/CLI出力に出す
   (`uv run python -m autoscreener.cli estimate-elasticity` の出力に
   「クランプ済み銘柄を含む場合/除いた場合」の2値を並べる)。
2. 2値が大きく異ならなければ(既存の断面間標準偏差0.117程度の範囲内)、
   現状のκの使用は妥当と判断してよい。乖離が大きい場合は、クランプ後の
   成長率ではなく `min(candidates)` の生値(クランプ前)を説明変数に
   使うよう `estimate_elasticity_over_history` を修正する
   (ランキング計算側の `base_initial_growth` は変更しない——クランプ自体は
   別の目的(外挿の暴走防止)で必要)。
3. どちらを採ったかを `config/scoring.yaml` の `growth_elasticity` の
   コメントに追記する。

### 受け入れ基準

- [ ] クランプ含む/除いたκの比較値が記録されている
- [ ] 乖離が小さければ「現状維持でよい」と明記して終了、大きければ生値ベースの再実装とテストを追加
- [ ] 変更する場合は `run-backtest` のKPIを確認する

---

<a id="e-5"></a>
## E-5【中】ランキング一覧に下振れ確率(P(半値以下)/P(元本割れ))が表示されない

### 何が起きているか

model_audit文書のC-1は「**ランキング表と銘柄詳細の両方に**」下振れ確率を
表示することを実装手順として明記している。バックエンドは
`probability_below_half` / `probability_below_one` を
`CandidateSummary`(一覧用)にも `CandidateDetail`(詳細用)にも正しく
格納している(`api/routes.py:397-402`)。ところが
`frontend/src/pages/RankingPage.tsx` はこの2フィールドを一度も参照して
おらず、詳細ページ(`TickerDetailPage.tsx:159-163`)にしか表示されていない。

### なぜ問題か

このアプリの一覧画面は利用者が**最初に、そして最も頻繁に**見る画面であり、
「下振れリスクを伝える」というC-1の目的は、まさにその画面で果たされる
べきものだった。バックエンドは毎回律儀に計算して返しているのに、一覧
画面では黙って捨てられている——値は存在するが**配線だけが欠けている**、
このプロジェクトで繰り返されてきたパターンそのものである。

### 修正指示(Sonnetへ)

`frontend/src/pages/RankingPage.tsx` のテーブルヘッダ(209-241行付近)と
行描画(244-299行付近)に、既存の `expected_moic` 列
(289行)・`survival_probability` 列(292-293行)と同じ書式で列を追加する:

```tsx
{/* ヘッダ側 */}
<th>下振れ(半値以下)</th>

{/* 行側。null許容で既存の書式に合わせる */}
<td>
  {item.probability_below_half != null
    ? `${(item.probability_below_half * 100).toFixed(1)}%`
    : "—"}
</td>
```

`frontend/src/api/types.ts` の `CandidateSummary` 型に
`probability_below_half` / `probability_below_one` が既に含まれているか
確認し(バックエンドが返している以上、型定義に無ければここも追加する)、
無ければ追加する。

### 受け入れ基準

- [ ] `RankingPage.tsx` に下振れ確率の列が表示される
- [ ] `CandidateSummary` 型定義に該当フィールドがある
- [ ] `cd frontend && npm run build` が型エラーなく通る
- [ ] ブラウザで一覧画面を開き、値が表示されることを目視確認する

---

<a id="e-6"></a>
## E-6【中】`/universe/status`(収集進捗)がフロントエンドから一度も呼ばれていない

### 何が起きているか

B-6(model_audit文書)は「`/universe/status` が実行途中の数字をそのまま
返し、完了したかどうか誤読される」問題を指摘し、その修正として
`run_started`/`run_finished` マーカーの追加(B-6実施済み、
`parallel_runner.py:58-67, 124-133`)が記録されている。バックエンドの
API(`fetchUniverseStatus`、`frontend/src/api/client.ts:84-86`)も存在
する。しかし**このAPIを呼び出すページ・コンポーネントがフロントエンドに
一つも無い**(`App.tsx`・`Layout.tsx` にルート/ナビリンクが無い)。

`frontend/src/api/types.ts` の型定義(114-120行)にも、収集進捗を表す
`collection_target_count` / `collection_complete` 相当のフィールドが
含まれていない。

### なぜ問題か

B-6が解決しようとした「利用者が収集の進捗を誤読する」という問題は、
**バックエンドのデータは直ったが、それを見る画面が無いためUI上では
未解決のまま**残っている。利用者はAPIを直接叩かない限り、日次収集が
完了したのか実行中なのかを知る手段がない。

### 修正指示(Sonnetへ)

1. `frontend/src/api/types.ts` に `/universe/status` のレスポンス型
   (`collection_target_count` / 進捗率など、バックエンドの実際のレスポンス
   スキーマを `api/schemas.py` で確認して合わせる)を追加する。
2. 既存のどこかの画面(トップページ・ランキング画面のヘッダ等、最小実装
   ならランキング画面上部のバナーで十分)に、収集状況を表示する小さな
   コンポーネントを追加する:
   - 実行中:「本日の収集を実行中です(N/M件)」
   - 完了:表示しない、または「本日のデータは最新です」
3. 新規ページを作る必要はない。既存レイアウトへの最小追加で良い
   (このプロジェクトはC-2〜C-7等の低優先度UI項目を意図的に後回しにする
   運用をしており、これも同程度の工数感で実装できる)。

### 受け入れ基準

- [ ] 何らかの画面から `/universe/status` が呼ばれ、結果が表示される
- [ ] 収集実行中に画面を開くと進行中である旨が分かる
- [ ] `npm run build` が通る

---

<a id="e-7"></a>
## E-7【中】DBバックアップに整合性検証・リストア手順が無い

### 何が起きているか

`src/autoscreener/batch/backup.py:24-39`

```python
dump = subprocess.run(
    ["docker", "compose", "exec", "-T", "db", "pg_dump", "-U", "autoscreener", "autoscreener"],
    cwd=_PROJECT_ROOT,
    capture_output=True,
    check=True,
)
with gzip.open(out_path, "wb") as f:
    f.write(dump.stdout)
```

`check=True` は `pg_dump` プロセスの**終了コード**のみを見る。極端に
小さい・空のダンプが出力されても(例:DBコンテナが起動していない状態で
接続だけ成立し空の出力が返るケース、権限エラーがstderrに出つつ終了コード
が0になるケース等)、サイズ・内容の検証なしに「成功」として保存され、
`_cleanup_old_backups`(14世代ローテーション)が**正常だった古い
バックアップを削除していく**。リストアを実行するコード・スクリプトは
リポジトリ内に存在しない(`restore` で全文検索してもヒットなし)。

### なぜ「根本的」か

このモジュールのdocstringは `scores`・`forward_returns` を「**再生成不可能な
検証資産**」と明記している。バックアップの目的そのものが、まさにこの
検証不可能性への備えである。**検証されていないバックアップは、いざ
という時に使えない可能性があるという意味で、実質的に「バックアップが
無い」のと同じリスクを抱えている。**

### 修正指示(Sonnetへ)

```python
# backup.py
def run_backup() -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    out_path = BACKUP_DIR / f"autoscreener_{utc_today().isoformat()}.sql.gz"

    dump = subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "pg_dump", "-U", "autoscreener", "autoscreener"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        check=True,
    )
    # E-7: 終了コードだけでは空/破損ダンプを検知できない。最低限の内容検証を行う。
    if len(dump.stdout) < _MIN_EXPECTED_DUMP_BYTES:
        raise RuntimeError(
            f"pg_dump output suspiciously small ({len(dump.stdout)} bytes) — refusing to save as backup"
        )
    if b"-- PostgreSQL database dump" not in dump.stdout[:1000]:
        raise RuntimeError("pg_dump output does not look like a valid dump — refusing to save as backup")

    with gzip.open(out_path, "wb") as f:
        f.write(dump.stdout)

    logger.info("backup written: %s (%d bytes)", out_path, out_path.stat().st_size)
    _cleanup_old_backups()
    return out_path
```

`_MIN_EXPECTED_DUMP_BYTES` は実際の直近バックアップのファイルサイズを
`backups/` ディレクトリで確認し、その1/10程度など余裕を持った値を根拠と
共に設定する。

さらに、**最低限のリストア確認手順**をドキュメント化する(コード変更
ではなく `README.md` への追記でよい。個人利用規模のプロジェクトである
ため、自動リストアテストのCI化までは過剰):

```markdown
## バックアップからの復元手順(確認済みであること)

1. `gunzip -c backups/autoscreener_YYYY-MM-DD.sql.gz | docker compose exec -T db psql -U autoscreener -d autoscreener_restore_test`
   (本番DBではなく一時DBへ復元してから検証する)
2. `SELECT count(*) FROM scores;` 等で件数が妥当か確認する
3. 最低でも四半期に1回、実際にこの手順を試すこと
```

### 受け入れ基準

- [ ] 空/極端に小さいダンプで `run_backup` が例外を送出する
- [ ] `tests/unit/` に(subprocessをモックした)回帰テストを追加する
- [ ] README にリストア手順が追記されている

---

<a id="e-8"></a>
## E-8【低】デシル分割の同値(タイ)処理が非決定的な挿入順に依存する

### 何が起きているか

`src/autoscreener/backtest/metrics.py:576` 付近、`_cross_sectional_buckets`
内の `sorted(by_date[base_date], key=lambda o: o.probability, reverse=True)`
はPythonの安定ソートであるため、確率が完全に同値の観測は、リストの
**入力順**(実質的に辞書の挿入順)でデシル所属が決まる。成長率上限・
粗利率フロア等、複数箇所でクランプが働く設計上、入力が実質同一になり
確率が完全一致する銘柄群は起こりうる。

### 影響

軽微。単調性・リフト等の主要KPIへの影響は無視できる規模と見込まれるが、
「同じ日に並んだ銘柄の中で上位ほどリターンが高いか」というデシル指標の
定義の厳密性をわずかに損なう。

### 修正指示(Sonnetへ)

同値のタイブレークにティッカーシンボル等の決定的な第二キーを使う
(観測が `ticker` を持つか確認して追加する):

```python
sorted(
    by_date[base_date],
    key=lambda o: (-o.probability, o.ticker),  # 確率降順、同値はticker昇順で決定的に
)
```

### 受け入れ基準

- [ ] 同値入力での並び順がテスト実行間で再現すること(既存の
      `tests/unit/test_backtest_metrics.py` に確認テストを追加)
- [ ] 修正前後で `run-backtest` のKPIがほぼ変わらないことを確認(変わる
      場合は同値が想定より多いことを意味するので原因を調べる)

---

<a id="e-9"></a>
## E-9【低】下振れ確率に用語集エントリ・ツールチップが無い

### 何が起きているか

model_audit文書のC-1実装手順は「`glossary.ts` に用語を追加して
`<Term>` でリンクする」ことを求めているが、`frontend/src/glossary.ts`
に下振れ確率のエントリが無く、`TickerDetailPage.tsx:161-162` の
「下振れ:P(半値以下)…」はプレーンテキストのままである。他の指標
(生存確率・期待倍率等)はすべて `<Term>` でツールチップ化されており、
この項目だけ用語集の仕組みから漏れている。

### 修正指示(Sonnetへ)

`glossary.ts` の既存エントリ(例:`survival_probability` の説明文)と
同じ形式で以下を追加する:

```ts
probability_below_half: {
  term: "下振れ確率(半値以下)",
  description: "7年後の実現倍率が0.5倍(投資額の半分)を下回る確率。生存確率と対数正規分布の下側で合成して算出。",
},
probability_below_one: {
  term: "下振れ確率(元本割れ)",
  description: "7年後の実現倍率が1.0倍を下回る確率。",
},
```

`TickerDetailPage.tsx:161-162` の該当テキストを `<Term>` コンポーネント
でラップする(E-5でランキング一覧にも列を追加する場合は、そちらの
ヘッダにも同じ `<Term>` を適用する)。

### 受け入れ基準

- [ ] `glossary.ts` にエントリが追加されている
- [ ] 詳細ページ(および一覧に追加した場合はそちら)でツールチップが表示される

---

## 実装順序の推奨

1. **E-1**(高、コアロジック)— 実データ調査(ステップ1)を最初に行う。
   AFYA/AMR型の「静かな有利誤読」は過去何度も上位ランクを歪めてきており、
   同じ形の欠陥を放置する理由が無い。
2. **E-2**(高、運用)— 1行変更で直る割に、放置すると本物の障害検知能力を
   蝕み続ける。優先度は高いが実装コストは最小。
3. **E-3**(中、運用)— E-2と合わせて監視系をまとめて手当てする。
4. **E-5・E-6・E-9**(中〜低、表示)— 実装済みのバックエンド機能をUIに
   配線するだけなので低コスト・高い効果。まとめて着手できる。
5. **E-7**(中、データ保全)— コード変更は小さいが、README追記(リストア
   手順の確認)を含めて実施する。
6. **E-4**(中、統計)— 変更ではなくまず検証(κの比較測定)から。結果次第で
   後続対応の要否を判断する。
7. **E-8**(低)— 余裕があれば。

いずれの修正も、モデルの挙動(ランキング順位)に影響するもの(E-1・E-4)は
**必ず `uv run pytest` → `uv run python -m autoscreener.cli run-backtest`
でKPIの変化を確認してからコミットすること**。これは本書が参照した2つの
先行監査文書が一貫して要求している、このプロジェクトの規律である。

---

## 対応状況(2026-08-27 実装)

`uv run pytest` = 321 passed / `cd frontend && npm run build` = 型エラーなし。

| # | 対応 | 残タスク(実データが必要・ここに追記すること) |
|---|---|---|
| E-1 | **診断フラグのみ(ステップ3)を実装。net_debt の計算式は未変更。** `point_in_time.py` で `Total Debt` / 現金の欠損を検知 → `MoicInputs.net_debt_data_missing` → `MoicResult` → `engine.result_to_factors` → API `_WARNING_RULES` → frontend `warnings.ts` の警告バッジまで配線。回帰テスト3件を `test_point_in_time.py` に、伝播テスト1件を `test_moic.py` に追加。 | ステップ1のSQL調査(欠損発生率)を実行し本節に記録 → ステップ2の恒久方針(None化 / 中央値フォールバック / 診断フラグのみ)を決定 → 挙動を変える場合は `run-backtest` でKPI確認。 |
| E-2 | **完了。** `monitoring.py` の成功率分子に `sanitized` を加算。加えて `sanitized` 比率が `SANITIZED_RATIO_WARN_THRESHOLD`(暫定0.30)を超えたら独立にWARNINGを出す。`test_monitoring.py` にテスト2件追加。 | 閾値0.30を実データ(`collection_logs` の `sanitized` 比率分布、平常時実測 約18.7%)で根拠づけて調整。過去ログで平常運転日に誤発火しないことを確認。 |
| E-3 | **完了。** `unhandled_error` を `DEFAULT_FAILURE_STATUSES` に追加。`test_parallel_runner.py` にサーキットブレーカー作動テスト追加(DBあり・passで確認済み)。 | — |
| E-4 | **検証機構を実装(挙動未変更)。** `estimate_elasticity_over_history` がクランプ銘柄を含む κ と除いた κ の両方を返す(新 `ElasticityCrossSection`)。CLI `estimate-elasticity` が2値・乖離・断面間標準偏差との比較・推奨アクションを出力。`moic.raw_initial_growth`(クランプ前 g0)を切り出し。 | `uv run python -m autoscreener.cli estimate-elasticity` を実データで実行し、2値と乖離を `config/scoring.yaml` の `growth_elasticity` コメント内「判定結果:」行と本表に記録。乖離が断面間標準偏差(0.117)を超える場合のみ説明変数を生値へ切替 → 再測定 → `run-backtest`。 |
| E-5 | **完了。** `RankingPage.tsx` に「下振れ(半値以下)」列を追加(`probability_below_half`、`<Term id="downside-probability">` 付き)。`CandidateSummary` 型は既に該当フィールドを保持。`npm run build` pass。 | ブラウザで一覧画面を開いて値の表示を目視確認。 |
| E-6 | **完了。** `types.ts` の `UniverseStatusResponse` に `collection_target_count` / `collection_complete` を追加(バックエンドは既に返却済み)。`CollectionStatusBanner` コンポーネントを新規作成し `RankingPage` 上部に配置(実行中のみ「本日の収集を実行中です(N/M件)」を表示)。`index.css` に最小スタイル追加。`npm run build` pass。 | 収集実行中に画面を開いて進行表示を目視確認。 |
| E-7 | **完了。** `backup.py` に空/破損ダンプ検証(`_MIN_EXPECTED_DUMP_BYTES` = 100,000、ヘッダ文字列チェック)を追加し、疑わしいダンプは `RuntimeError` で保存拒否。`tests/unit/test_backup.py` を新規作成(subprocessモック、4ケース)。README に「バックアップからの復元手順(四半期に1回は実際に試すこと)」を追記。 | `_MIN_EXPECTED_DUMP_BYTES` を実際の直近バックアップサイズ(`backups/`)を見て確定。四半期ごとの復元テストを運用に組み込む。 |
| E-8 | **完了。** `metrics.py` の `_cross_sectional_buckets` のソートキーを `(-o.probability, o.ticker_id)` にし、同値タイを決定的に解消。`test_backtest_metrics.py` に入力順非依存テスト追加。 | 修正前後で `run-backtest` のKPIがほぼ変わらないことを確認(大きく変わるなら同値が想定より多い)。 |
| E-9 | **完了。** `glossary.ts` に `downside-probability` エントリを追加(既存スキーマ `id/term/aliases/category/short/body/example` に準拠。監査案の `term/description` 形式ではなく実スキーマに合わせた)。`TickerDetailPage.tsx` の「下振れ」を `<Term id="downside-probability">` でラップ。E-5でランキング列ヘッダにも同じ `<Term>` を適用。`npm run build` pass。 | — |
