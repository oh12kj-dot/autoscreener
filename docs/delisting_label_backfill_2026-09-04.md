# 上場廃止イベントの原因分類バックフィル — 調査記録・実装範囲

**作成:** 2026-09-04〜09-05 JST
**対象:** `delisting_events`(94件、全件 `event_type='unknown'`)/ `ticker_aliases`(0件)
**背景:** `docs/racr_integrated_redesign_plan_2026-09-04.md` WP-F(competing-risk・
Permanent Loss)は原因区分と決済額が無いと学習できない
(監査 `autoscreener_racr_integrated_redesign_audit_2026-09-04.md` §3.4/§5.3)。
**基準ブランチ:** `worktree-agent-a6b37e450739feece`(`main` の `622cb83` から分岐、
その後 `main` は WP-F1 まで進んでいるがこの作業は追随していない——後述)。

**先に結論:** 現在DBにある証拠だけでは94件中 **0件** が新たに分類できる
(推測ではなく実測)。それでも実装したコードは無駄ではない——「収集の設計が
原因を捨てている」という事実そのものが本調査の主な成果であり、これを直さない
限り94件が0件のままなのは今後も同じである。以下、根拠を1件ずつ積み上げる。

---

## 0. 調査方法

すべて **dev DB `autoscreener` への読み取り専用クエリ**(SQLAlchemyの生SELECT、
書き込みなし)と、既存コードの読解で確認した。新規のEDGARネットワーク取得は
一切行っていない。テストは `TEST_DATABASE_URL` 経由の専用DBのみ使用した。

## 1. 94件の実体(実測)

```sql
SELECT source, count(*), min(event_date), max(event_date),
       count(*) FILTER (WHERE last_trade_price IS NOT NULL) AS has_ltp,
       count(*) FILTER (WHERE settlement_value_per_share IS NOT NULL) AS has_settlement,
       count(*) FILTER (WHERE source_url IS NOT NULL) AS has_url
FROM delisting_events GROUP BY source;
```

結果(2026-09-04時点):

| source | 件数 | event_date範囲 | last_trade_price | settlement | source_url |
|---|---:|---|---:|---:|---:|
| `ticker_master_backfill` | **94**(全件) | 2022-07-29 〜 2026-08-27 | 0 | 0 | 0 |
| `sec_full_index` | **0** | — | — | — | — |

**発見1:94件は全件 `ticker_master_backfill` 由来であり、`sec_full_index`
(SECフルインデックスのForm 25/15走査、`collectors/delisting_source.py` の
`register_delisting_events`)が書いた行は1件も無い。** この収集経路が過去に
一度も本番投入されていない(または投入結果が別の事情で全消去された)ことを
意味する。94件は一度もSECの一次証拠(提出フォーム種別・accession番号・
提出日)を持ったことがない。

`batch/collect_delistings.py:backfill_delisting_events_from_tickers` の実装を
読むと、`event_date` の実体は `tickers.delisted_at` である。そして
`delisted_at` を書き込む箇所は3つ(`collectors/snapshot_collector.py:571,583`、
`collectors/delisting_source.py:263,269`)のうち、実際に発火していたのは
yfinance側の失敗シグナルだけだった:

```python
# collectors/snapshot_collector.py:568-573
try:
    payload = fetch_raw_financials(symbol, collection_config.retry, ...)
except PermanentFailure as exc:
    ticker.delisted_at = datetime.now(UTC)   # ← 即座に確定、SEC照合なし
    ...
```

**発見2:`event_date` は「SECが上場廃止を届け出た日」ではなく「yfinanceが
404等の恒久失敗を返した日」である。** 実例(LXU、`consecutive_failures=0`)で
確認済み——`PermanentFailure` は1回の失敗で即座に `delisted_at` を確定させる
分岐であり、独立した規制当局の確認を経ない。データベンダー側の一時的な障害
(ティッカー変更、配信停止、API仕様変更)と本当の上場廃止を、この時点では
区別できていない。

## 2. filings テーブルとの突合(実測)

`filings` は208,027行ある。94件の廃止銘柄のCIKで突合を試みた。

```sql
-- 誤ったやり方(cikでJOIN)
SELECT count(DISTINCT de.ticker_id) FROM delisting_events de
JOIN tickers t ON t.id = de.ticker_id
JOIN filings f ON f.cik = t.cik WHERE t.cik IS NOT NULL;
-- => 2

-- 正しいやり方(ticker_idでJOIN——filings.ticker_id は収集時に確定している)
SELECT count(DISTINCT de.ticker_id) FROM delisting_events de
JOIN filings f ON f.ticker_id = de.ticker_id;
-- => 1
```

**発見3(重要、CIK共有の罠):`cik` でJOINすると2件ヒットするが、
`ticker_id` の正しいJOINでは1件しかヒットしない。** 実例:

| symbol | ticker_id | cik | delisted_at |
|---|---:|---|---|
| `TDW`(現役) | 4647 | 0000098222 | NULL |
| `TDGMW`(廃止) | 13963 | 0000098222 | 2023-07-31 |

`TDGMW`(倒産再編後のワラント類と見られる)は現役銘柄 `TDW`(Tidewater Inc.)
と同じCIKを共有している。`cik` でJOINすると、TDGMWが実際には一度も自分の
`ticker_id` に対して収集したことのない、TDWの通常運用中の提出書類
(2014年〜2026年9月まで継続、直近も8-Kが出続けている)が「TDGMWの廃止証拠」
として誤って引っかかる。**もし分類ロジックを `cik` でJOINして実装していたら、
現役で全く問題のない企業の書類を根拠に、無関係な廃止銘柄へ因果を捏造して
いたことになる。** これは本調査で最初に書いたコードが実際に踏んだ罠であり
(後述の実装で `ticker_id` のみJOINに修正済み)、監査 §5.3 の「推測しない」
原則を型のレベルで破りかねない具体例として記録する。

`ticker_id` で正しく突合できる1件(LXU)の中身も確認した:

```sql
SELECT form, filed_date, items FROM filings
WHERE ticker_id = (SELECT id FROM tickers WHERE symbol='LXU')
ORDER BY filed_date DESC LIMIT 20;
```

765行あるが、直近(delisted_at=2026-08-25の5日前まで)のフォームは
`8-K`(Item 7.01, 8.01, 5.02, 5.03, 4.01, 5.07, 2.02, 9.01)のみ。
**原因を語るItem(1.03=破産、2.01=買収完了、3.01=上場廃止通知)も、
Form 25/15そのものも1件も無い。** LXUの`delisted_at`後は価格スナップショット
も途絶えている(`max(trade_date)=2026-08-24`)ため、少なくとも取引停止自体は
本物である可能性が高いが、原因の手がかりは皆無。

## 3. なぜfilingsに証拠が無いのか(設計上の理由、実測で確認)

```python
# batch/run_daily_collection.py:44-47
.filter(
    # 上場廃止は一時的な隔離ではなく永続的な取引不能状態。隔離の
    # 再挑戦期限が来ても収集対象へ戻してはいけない。
    Ticker.delisted_at.is_(None),
    ...
)
# batch/collect_filings.py:100-105 も同じガード
.filter(Ticker.cik.isnot(None), Ticker.delisted_at.is_(None), Ticker.is_benchmark.is_(False))
```

**発見4:`delisted_at` が立った瞬間、そのCIKの `filings` 収集は永久に止まる。**
これは意図的な設計(もう取引されない銘柄を毎日ポーリングしても無意味)だが、
副作用として「原因を語るはずのフォーム」——Form 25/15そのもの、8-K Item
1.03/2.01/3.01、DEFM14A、Schedule 13E-3——は**まさに廃止が確定するタイミング
の前後に集中して提出される**にもかかわらず、その時点で収集が止まっているため
ほぼ確実に取りこぼす。94件中93件が `filings.ticker_id` に1行も持たない事実
(§2)は、この設計の直接の帰結であり、偶然のデータ欠落ではない。

追加で、`batch/collect_filings.py:36-61` の `TRACKED_FORMS` を確認したところ、
**`SC 13E3`(非公開化届出書)や `DEFM14A`(合併委任状)はそもそも保存対象
フォームの一覧に入っていない。** つまり廃止前に収集が生きていたとしても、
これらの決定的な証拠フォームは最初から `filings` に入らない設計になっている。

## 4. ライブ取得すれば直るか(コード読解のみ、実行せず)

`collectors/edgar_client.py:216-223` の `fetch_filings` は `submissions` API
の `filings.recent` だけを読む:

> `filings.recent` は列指向の並列配列...古い分は `filings.files[]` に別JSON
> へのポインタが入るが、本計画では `recent` だけを使う(30.3.1:直近1000件・
> 約1年分あれば十分)

**発見5:提出頻度の高い発行体では、廃止から時間が経つほど当該Form 25/15
自体が `recent` ウィンドウ(直近1000件)から外れて取得不能になる。** 今回の
94件は `event_date` が2022-07〜2026-08に分布しており、古いものほどこの限界に
かかる。ライブ取得を実装しても万能の解決にはならない——「廃止直後に取りに
行けば拾える」ケースと「もう`recent`から落ちている」ケースが混在する。

**このセッションでは制約により実際のライブ取得は行っていない**(テストは
外部通信を遮断、dev DBへの書き込みも禁止)。これは推測ではなく明示的に
未検証のまま残す。

## 5. `ticker_aliases` が0件である理由(実測、結論:バックフィル対象ではない)

`ticker_aliases` の書き込み口は1つだけ存在する:

```python
# collectors/snapshot_collector.py:276-305 _reassign_ticker_for_symbol_reuse
# シンボル再利用(ISIN不一致で別会社と判明)を検知したときだけ発火し、
# 旧tickerのsymbolを "<symbol>~D<id>" 退避してticker_aliasesへ書く。
```

このロジックが発火した形跡(`symbol LIKE '%~D%'` のティッカー)は dev DB に
**0件**。また、複数のティッカーが同一CIKを共有しているケース(`AGM-A`/`AGM`、
`LBTYA`/`LBTYB`/`LBTYK` 等、20件超確認)はすべて**優先株/複数クラス株**であり
シンボル改名の痕跡ではない。

**結論:`ticker_aliases` の0件は「書き込み口が壊れている/未接続」ではなく
「発火条件(シンボル再利用+ISIN不一致)がまだ一度も起きていない」ことの
正しい反映である。** DBの中に「使われていないシンボル改名の証拠」が眠って
いるわけではないため、**このタスクでは `ticker_aliases` 用のコードは追加
しない**——追加しても書き込むべき実データが無いことを既に確認済みであり、
書けば埋めるものが無いのに存在だけする空の仕組みになる。監査の「0行=空白」
という指摘は、原因(未発火 vs 未接続)まで見ると後者ではなく前者だった、
というのが本調査の訂正である。

## 6. 実装したもの

### 6.1 taxonomyは新規に作らない(重要な軌道修正)

最初の実装では `bankruptcy`/`acquisition`/`going_private`/
`exchange_deficiency`/`voluntary_deregistration` という独自の5値+`unknown`を
作った。**これは誤りだった。** 全テストスイートを流したところ、既存の
`backtest/runner.py:472-489`(実現リターン計算)と `api/routes.py` のM&A履歴
エンドポイントが、既に次の6値を消費していることが判明した:

```python
# backtest/runner.py:479-489
if event_type == "cash_acquisition" and settlement is not None:
    return (settlement + dividends) / entry - 1, "cash_acquisition"
if event_type == "stock_acquisition" and settlement is not None:
    return (settlement + dividends) / entry - 1, "stock_acquisition"
if event_type in {"cash_acquisition", "stock_acquisition"}:
    return -1.0, "unknown_delisting"          # ← 決済額が無いと "-100%" 扱い
if event_type in {"bankruptcy", "liquidation"}:
    recovery = settlement or 0.0
    return (recovery + dividends) / entry - 1, event_type
if event_type in {"exchange_transfer", "unknown"}:
    return -1.0, "unknown_delisting"
```

`unknown` / `cash_acquisition` / `stock_acquisition` / `bankruptcy` /
`liquidation` / `exchange_transfer` が正しいtaxonomyであり、`EVENT_TYPES` は
これに合わせて修正した。独自の値を書けば、この既存ロジックはその値を知らず
無視するか、意図せぬ分岐に落ちる。

**さらに重要な発見:上のコードを読んで初めて、`cash_acquisition`/
`stock_acquisition` を決済額(`settlement_value_per_share`)無しで書き込むと、
本来は利益で終わったはずの買収イグジットが丸ごと「-100%の損失」として
実現リターンに算入されることが分かった。** これは監査 §5.3 が警告する
「決済不明を0として学習しない」の**具体的な失敗モード**そのものである。
したがって:

- 8-K Item 2.01(取得完了)やSchedule 13E-3(非公開化)の証拠が見つかっても、
  **決済額が無い限り `cash_acquisition`/`stock_acquisition` へは自動分類しない**。
  証拠自体(フォーム・URL・提出日)はrationaleに残し、人間が決済額を追加で
  調べれば手動確定できるようにする。
- `exchange_transfer` も、既存コードで `unknown` と数値上全く同じ扱い
  (`-1.0, "unknown_delisting"`)になるため、8-K Item 3.01(上場廃止通知)
  だけから自動的に付ける実益が無い——「原因が分かった」という誤った印象だけ
  残るので付けない。
- **自動分類するのは `bankruptcy` のみ**(8-K Item 1.03=Bankruptcy or
  Receivership)。これはSEC規則上、破産手続き開始という一次的で明確な事象
  であり、決済額欠落時の `recovery = settlement or 0.0` という保守的な扱いは
  `runner.py` に既に存在する(本モジュールが新規に導入したのではない)。

### 6.2 新規モジュール `src/autoscreener/collectors/delisting_classification.py`

- `EVENT_TYPES`:上記6値のタプル(§6.1参照)。
- `classify_from_filings(evidence) -> Classification`:純粋関数。証拠から
  `bankruptcy` を判定する以外は常に `unknown` を返す設計(§6.1の理由)。
  証拠が全く無い場合と「証拠はあるが安全に確定できない」場合を rationale で
  区別する。
- `gather_evidence_for_event(session, event, ticker)`:`filings` を
  **`ticker_id` でのみ**JOINする(§2の罠を踏まないため)。同じCIKを持つ
  他の現役銘柄が存在する場合は `ambiguous_shared_cik=True` を立てる。
- `classify_stored_delisting_events` / `apply_classifications`:
  DB書き込みは`event_type != 'unknown'` かつ `ambiguous_shared_cik=False`
  の場合のみ行う。CIK共有で疑わしいものは自動では書かず、
  `ambiguous_shared_cik_skipped` として別カウントする。

### 6.3 CLI: `classify-delistings`

`rollback-false-delistings` と同じ作法——常に内訳(件数・代表例)を出力し、
`--apply` が無ければ `typer.BadParameter` で止める(書き込みは常に明示的な
再実行を要求)。`--dry-run` は追加のオプションではなく既定動作そのもの。

### 6.4 migration `b3f6d1a08c92_delisting_event_type_check.py`

`delisting_events.event_type` に CHECK制約を追加し、許容値を上記6値
(`unknown`含む)に固定する。`down_revision = c80f29dab3b6`(本作業のブランチが
分岐した時点のalembic head)。**`c80f29dab3b6`にchainする単一migrationとして
実装・検証済み**(upgrade/downgrade双方をisolateした形で確認)。

**マージ時の注意(このセッションで実際に発生した問題を記録):** 本ブランチは
`main` の `622cb83` から分岐しており、その後 `main` は WP-F1 (`a1e065a`) まで
進んでいる。共有のテスト用PostgreSQLインスタンスを他ブランチの作業と同時に
使っていたため、本migrationを一度 `alembic upgrade head` した際に共有DBの
`alembic_version` が一時的に本migrationのrevisionへ進み、`main` 側の作業を
妨害した(既に是正済み・後述§8)。**もし `main` がこの間に `c80f29dab3b6` の
後へ別のmigrationを追加していた場合、本migrationはそちらへ向けてrebaseし
直す必要がある**(down_revisionの付け替えのみで、内容自体の変更は不要)。
これは本ブランチをマージする側が対応すべき既知の作業として明記しておく。

### 6.5 テスト `tests/unit/test_delisting_classification.py`(14件)

- `classify_from_filings` の純粋ロジック:証拠無し→unknown、bankruptcy確定
  (corroboration有無で confidence high/medium)、Schedule 13E-3/Item 2.01/
  Item 3.01は証拠ありでも決済額が無いためunknownのまま、Form 15単独では
  原因を主張しない、bankruptcy優先順位、`EVENT_TYPES`が既存消費側と一致する
  ことの固定テスト。
- DB連携の回帰テスト:**`TDW`/`TDGMW`のCIK共有パターンをそのまま再現**し、
  `cik`でのJOINを使えば誤帰属することを踏まえて `ticker_id`だけを使う実装が
  正しく空の証拠を返すこと、`ambiguous_shared_cik`が正しく立つことを確認。
- end-to-end:bankruptcy証拠→`classify_stored_delisting_events`→
  `apply_classifications`→DB反映まで一気通貫。dry-runが書き込まないこと。
  CIK共有で証拠自体は本物(誤帰属ではない)でも自動適用されないこと。

## 7. 実測されたテスト結果

**本ブランチは `main` の `622cb83` から分岐しており、`main` は現在WP-F1
(`a1e065a`)まで進んでいる。以下の数字は本ブランチ自身のもので、`main`の
現在の基準(1176 passed / 0 failed)とは前提が異なる——WP-F1のテストは
本ブランチには存在しない。**

```
TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test \
python -m pytest -q
```

- 新規migration `b3f6d1a08c92` を一時的に適用した状態(ローカルのmigration
  ファイル群とDBの `alembic_version` が一致):**1157 passed, 0 failed**
  (既存1143 + 新規14)。
- その後、共有DBを `main` が期待する `c80f29dab3b6` へ戻した状態(本migration
  ファイルはこのブランチのworking treeに残るが、共有DBには未適用)で再実行:
  **1156 passed, 1 failed**。失敗は
  `test_operational_readiness.py::test_healthy_recent_run_and_fresh_data_is_ready`
  の1件のみで、原因は `alembic_head_mismatch`——ローカルの `alembic/versions/`
  を走査して求めた「期待head」(本migrationがまだcommitされていないworking
  treeにしか無いため `b3f6d1a08c92`)と、共有DBの実際のstamp
  (`c80f29dab3b6`)が一致しないことを検知する既存のreadinessチェックが
  正しく動作した結果であり、**私が書いたどのコードの機能的な不具合でもない**。
  複数ブランチが同一のPostgresインスタンスを共有しているために生じる環境要因
  であり、本migrationがcommit・適用されて一貫すれば自然に解消する(§6.4の
  マージ注意と同じ根本原因)。**新規に追加した14テストは、この状態でも
  全件パスしている**——DB側のCHECK制約の有無に依存しない設計にしたため。

**共有テストDBへの後始末:** 作業終了時点で共有 `autoscreener_test` の
`alembic_version` は `c80f29dab3b6`(`main`が期待する状態)に戻し、
本migrationが一時的に追加したCHECK制約も明示的にDROPして原状回復した——
上の1156/1157の数字の差はこの後始末そのものが原因であり、隠さずそのまま記録
する。
本migrationファイル自体はこのブランチにのみ存在し、コミットするが、共有DBは
コミット前の状態のまま残している(§6.4のマージ時注意を参照)。

## 8. dev DB(read-only)への影響

このセッションを通じて `autoscreener`(dev DB)への書き込みは一切行っていない。
最終確認:

```
delisting_events: 94件(変化なし)
ticker_aliases:    0件(変化なし)
alembic_version:   c80f29dab3b6(変化なし)
```

`autoscreener_readonly` ロール(18.6、`scripts/create_readonly_role.sql`)の
存在は確認したが、本セッションの調査クエリは(先に確立していた)通常の
`autoscreener` 接続で読み取り専用SELECTのみ発行しており、書き込み権限を
一度も行使していない。

## 9. 実際に得られる分類カバレッジの現実的な上限

| 条件 | 94件中の該当数 |
|---|---:|
| 現在DBの `filings` から `ticker_id` で正しく突合できる | 1件(LXU) |
| そのうち原因を語るItem/フォームを持つ | 0件 |
| **したがって現時点で自動分類できる件数** | **0件** |
| ライブEDGAR取得を実装・実行した場合の理論上限 | 未検証(§4:`recent`ウィンドウの制約あり、廃止直後のものほど有利) |

**「94件中0件」は失敗ではなく、このタスクの主要な出力である。** 収集設計
(delisted_at確定と同時に収集停止、TRACKED_FORMSに主要フォームが未登録、
CIK共有を考慮しないJOINは誤帰属の危険)を直さない限り、原因分類は今後も
実質ゼロのままである。次にこの領域へ着手する者への実務的な示唆:

1. `delisted_at` が確定する**前**の猶予期間(例:確定後30〜90日)だけ、その
   CIKの `filings` 収集を継続する例外を `collect_filings.py`/
   `run_daily_collection.py` に設ける(Form 25/15・8-K Item 1.03/2.01/3.01が
   出るのはまさにこの窓)。
2. `TRACKED_FORMS` に `SC 13E3`・`DEFM14A`・`15F-12B`・`15F-12G` を追加する。
3. 決済額(cash/stock consideration)を安全に埋めるには、フォーム本文の
   パース(現状 `Filing` は本文を保存しない設計)か、DEFM14A/8-K Item 2.01の
   本文からの構造化抽出が別途必要——本タスクの範囲外。
4. `ticker_aliases` は現状のままでよい(§5)。

---

## 変更ファイル

| ファイル | 変更 |
|---|---|
| `src/autoscreener/collectors/delisting_classification.py`(新規) | 分類ロジック・証拠収集・DB反映 |
| `src/autoscreener/batch/collect_delistings.py` | `classify_delistings()` オーケストレーション追加 |
| `src/autoscreener/cli.py` | `classify-delistings` コマンド追加 |
| `alembic/versions/b3f6d1a08c92_delisting_event_type_check.py`(新規) | `event_type` CHECK制約(6値+unknown) |
| `tests/unit/test_delisting_classification.py`(新規) | 14テスト(§6.5) |
| `docs/delisting_label_backfill_2026-09-04.md`(本ファイル) | 調査記録 |
