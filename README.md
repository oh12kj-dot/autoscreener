# TENX

> **2026-08-31 operation record:** the scheduled pipeline finished `degraded`.
> Collection produced 303 successful and 109 sanitized results, but 5,481 of
> 5,893 tickers remained quarantined and scoring was skipped because only 1.8%
> of the 1,188 gated tickers had a latest-date price row. The consensus and
> investment-intelligence stages also failed; their collector bugs are fixed in
> code, but a complete scheduled rerun has not yet verified the full pipeline.
> Quarantine recovery remains an explicit operational step (see J-0 below).

yfinanceの定量データから、小型〜中型の米国株を対象に将来10倍株(10バガー)になる可能性が相対的に高い銘柄をスクリーニング・スコアリングするアプリです。詳細な設計・実装の経緯は [`docs/10bagger_app_requirements.md`](docs/10bagger_app_requirements.md)、文書全体の案内は [`docs/README.md`](docs/README.md) を参照してください。

**スコアは0〜100の点数ではなく `P(7年で10倍)` という確率です。** 株価を恒等式
`売上 × 利益率 × マルチプル ÷ 発行済株式数` に分解し、4因子をそれぞれ7年後まで
外挿して掛け合わせ、不確実性と生存確率を載せて閾値超過確率にしています(要件定義書27章)。
小型株が10倍になる基準率は1%未満なので、上位銘柄でも数%にしかなりません。

**画面には性質の違う2つの確率が並びます。**

| 数字 | 何か | 検証できるか |
|---|---|---|
| `P(7年で10倍)` | 恒等式モデルが出す閾値超過確率。ランキングのキー | **できない**(7年後の実測は今日存在しない)。序列として読む |
| `1年オンペース率` | 今後1年で年率38.9%(=10倍/7年と同じペース)に乗る確率 | **できる**。擬似バックテストの実測頻度で較正済み |

較正は単調写像なので順位は変えません。変わるのは水準だけです。

> ⚠ **モデルの現状(v4、2026-08-26 の欠陥修正後)。** 14.2が主指標とするデシル単調性は
> **+0.806**、**断面リターン上位5%の事象に対するリフトは 2.19**(上位2%なら3.03)で、
> 目標の2.0に到達しています。ただし**上位10%・上位2%の事象では、8評価日のうち
> いくつかで上位デシルがユニバースを下回っており、常に効くモデルではありません**。
> ランキングを見る前に `/validation`(モデルの検証状況)を必ず確認してください。
>
> 単調性が以前の表示(+0.830〜+0.842)より低いのは、モデルが悪化したからでは
> ありません。**デシルを評価日をまたいでプールして切っていた指標側の欠陥を直した**
> ため、確率の水準が高かった評価日の銘柄が上位デシルを占める効果が消えました
> (同時にリフト倍率は 1.32 → 1.51 に是正されています)。詳細は D-4 を参照。

> 🔍 **既知欠陥の状況(2026-08-26)。**
>
> - **修正済み** — コードを直接読み直して見つけた10件の欠陥を修正しました。
>   通貨混在によるEV計算の誤り(262銘柄・5.0%が影響、AFYAが総合17位→83位)、
>   分割調整済みの株式数と未調整の株式数の混在(134銘柄・2.5%、RKTが希薄化
>   184%/年→15%/年で誤除外から復帰)、任意ホライズン再計算での確率の過大表示
>   (「3年で3倍」が中央値1.11倍過大)、無効化されたまま有効だと文書化されて
>   いた安全装置(BRUNが9位→18位)など。
>   全件の原因・実測影響・修正内容・**残課題6件**は
>   [`docs/defect_fixes_2026-08-26.md`](docs/defect_fixes_2026-08-26.md) にあります。
>   **とくに残課題 R-1(バックテストの生存バイアス)は全KPIを実態より
>   良く見せているので、`/validation` を読む前に把握してください。**
> - **一部未着手** — 実データでランキング上位を逆解析した監査により、
>   **上位30銘柄はモデルの外挿限界(クランプ)に当たった銘柄に偏っている**ことが
>   分かっています。全26項目の内訳・原因・修正案・実装手順は
>   [`docs/model_audit_v4_2026-08-26.md`](docs/model_audit_v4_2026-08-26.md) にあります。
>
> **モデルに手を入れる前に、両方とも必ず読んでください。**

> 🧭 **投資判断の周辺機能(2026-08-29、[`docs/investment_decision_gap_2026-08-29.md`](docs/investment_decision_gap_2026-08-29.md))。**
> 銘柄詳細に「会社の姿」「実績の推移(売上・粗利率・CF・現金・株式数・ランウェイ・
> F-score内訳)」「バリュエーションの現在地(断面分位・52週位置)」「実現倍率の
> 分位点(P10–P90)」「デューデリ・チェックリスト(11工程)」「需給(インサイダー・
> 空売り残・浮動株)」「次回決算日」を追加しました。カレンダー画面(`/calendar`)、
> 保有画面の売却規律(達成倍率・利食い計画・テーゼ点灯)、円換算トグルも追加。
> **いずれも表示・記録層で、順位計算(`probability`)は一切動かしていません。**

> 🧭 **投資判断の情報表示(2026-08-30、[`docs/investment_information_gap_2026-08-30.md`](docs/investment_information_gap_2026-08-30.md))。**
> 銘柄詳細に実測リスク(ボラティリティ・ドローダウン・β)、決算サプライズと予想改訂、
> 経営陣・保有構成、提出書類時系列、同業比較、推定の足場、執行日数、顧客集中・
> ガイダンス・訴訟の取得状態を追加しました。保有画面では残余必要倍率・必要CAGRと
> 実測相関を表示します。これらは既存データの表示専用で、`probability` は変更しません。
> 生存バイアス回復とEDGAR収集の実行はDB全体を更新する運用作業のため、手順を確認して
> 明示的に実行してください。
> J-6/J-7 のデータ収集は `collect-events` / `collect-insider` / `collect-short-interest`
> で行いますが、EDGAR/FINRA の取得経路はまだ薄く、実データが入るまで各項目は
> 「未取得」表示になります。円換算は `collect-macro`(FRED `DEXJPUS`)が前提です。
> **モデル検証の回復(J-0:`recover-quarantine` → `collect-delistings` →
> `run-backtest`)はまだ実行していません。** 上の R-1 の警告は引き続き有効です。

> 📐 **投資理論に基づく再監査(2026-08-30、[`docs/investment_theory_review_2026-08-30.md`](docs/investment_theory_review_2026-08-30.md))。**
> 実装の欠陥ではなく「モデルが投資理論として何を主張しているか」だけを見た監査です。
> 見つかった主要な問題は3つ:
> ① **点推定が景気循環のどの局面で観測したかに依存している**——粗利率トレンドも
> 初期成長率も直近の観測をそのまま7年へ引き伸ばす推定量で、v4はこれを σ でしか
> 扱っておらず、その σ は85%縮小される(断面SD 0.182 → 0.023)。結果として
> 素材・エネルギーが母集団の11.6%しかないのに上位50の40%を占めていました。
> ② **自社株買いを7年複利で外挿**——下限に張り付いた銘柄が無償で1.43倍を受け取り、
> 上位50の14%が該当。③ **ランキングが入口バリュエーションと正の相関**
> (`corr(P, ln(EV/粗利)) = +0.176`)。
>
> ①②と、③に対する終端倍率の上限は修正して**出荷設定に入れました**(30.1〜30.4)。
> ③の本丸(成長調整後の割高・割安を戻す)は実装・測定したうえで**採用していません**
> ——1年バックテストで3KPIとも一貫して負に出たためです(30.5、既定 0.0)。
> **どの変更も `compare-configs` の95%CI内(INDISTINGUISHABLE)です。** これは
> D-2(有効標本が実質3)がそのまま効いているためで、採否の根拠はKPIではなく
> 「推定量のバイアスを直しているか」に置いています。
>
> **直っていないこと**:①で挙げたセクター集中は**解消していません**
> (上位50の素材+エネルギーは 11 → 12)。一致度の実測では Basic Materials は
> 売上 0.95・粗利率 0.73 と平均より単調で、**年次3〜5期という窓が資源の
> 1サイクル(5〜10年)より短いため、循環が構造に見えている**のが原因です。
> モデルではなくデータの制約であり、`collect-xbrl`(10年超の年次)が動けば
> 窓が倍以上になります。詳細と対処案は同文書 §5.2。

---

## 前提条件

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)(起動時にログイン時自動起動を有効にしておくと日次自動実行が安定します)
- Python(`uv`で管理。[uv](https://docs.astral.sh/uv/)がインストール済みであること)
- Node.js 20以降(フロントエンド用)

## 初回セットアップ

```bash
# 1. 環境変数ファイルを作成(DB接続情報)
cp .env.example .env

# 2. Postgresを起動(ヘルスチェック通過まで待機)
docker compose up -d --wait

# 3. Python依存関係のインストール
uv sync

# 4. DBスキーマを作成
uv run alembic upgrade head

# 5. APIレイヤー用の読み取り専用DBロールを作成(初回のみ)
#    scripts/create_readonly_role.sql の CHANGE_ME を実際のパスワードに置き換えてから実行し、
#    .env の API_DATABASE_URL にも同じパスワードを設定する
docker compose exec -T db psql -U autoscreener -d autoscreener < scripts/create_readonly_role.sql

# 6. フロントエンドの依存関係インストール
cd frontend && npm install && cd ..
```

> **第30章「TENXの外側」の機能(EDGAR連携・マクロ・保有管理)を使う場合**、
> `.env` に `EDGAR_USER_AGENT`(SECが要求する連絡先つきUser-Agent)を
> 追加で設定してください。`FRED_API_KEY`(マクロ機能用、無料発行)は任意
> ——未設定でも他の機能はすべて動きます。詳細は後述の節を参照。

## 日常の使い方

### Web UIを見る

バックエンドとフロントエンドをそれぞれ別ターミナルで起動します。

```bash
# ターミナル1: API(http://localhost:8000 、Swagger UIは /docs)
uv run uvicorn autoscreener.api.main:app --port 8000 --reload

# ターミナル2: フロントエンド(http://localhost:5173)
cd frontend && npm run dev
```

ブラウザで `http://localhost:5173` を開くと以下の画面があります。

| 画面 | パス | 内容 |
|---|---|---|
| ランキング一覧(Tier 1) | `/` | 確率の降順。**「何年で何倍」を自由に指定**でき(3年で3倍、5年で5倍など)、セクター・時価総額でもフィルタ可能。表示中の銘柄を**まとめて持った場合の見通し**(少なくとも1つ当たる確率)も出る |
| 監視リスト(Tier 2) | `/watchlist` | ランキングに出ないが追跡する価値のある銘柄。ゲート1つ未達・新規上場・ランキング対象外(測定不能または見通しマイナス)の3分類 |
| 銘柄詳細 | `/candidates/{ティッカー}` | 5因子の内訳(各因子が実現倍率を何倍にしているか)・診断値・確率の推移 |
| 除外銘柄確認 | `/excluded` | ゲートで除外された銘柄を理由で検索 |
| 順位変動 | `/rank-changes` | 直近2日分のスコアを比較し、新規ランクイン・急上昇銘柄を表示 |
| 用語集 | `/glossary` | **マルチプル・希薄化・デシル・CAGR・YoY など52語の解説。** 具体的な数字の例つき。画面上で点線の下線が付いた言葉はマウスを乗せる(タップする)とその場で説明が出て、「くわしく →」でここへ飛べる |
| スコアについて | `/reference` | 4因子の算出式と、確率に変換する手順の説明 |
| モデルの検証状況 | `/validation` | 擬似バックテストの結果(KPI・右裾リフト・評価日ごとの内訳・較正曲線・留保事項) |

> **専門用語が分からないときは。** 画面上の点線の下線が付いた言葉にマウスを乗せる(タッチ端末ではタップする)と、
> その場で1行の説明が出ます。まとめて読むなら `/glossary`(用語集)へ。
> 用語の意味が分からないままランキングだけを見るのは、**モデルを信じてよいか判断できない状態**と同じです。
>
> **確率の読み方。** 上位銘柄でも「7年で10倍」の確率は数%であり、**大半は外れます**。
> 順位は7年後までの外挿にもとづく推定であって、当たりの予告ではありません。
> 一次スクリーニングとして使い、最終判断は定性分析(事業内容・経営者・モート)を
> 行った上で下してください。
>
> **目標は自由に変えられます。** ランキング画面で「3年で3倍」「5年で5倍」などを指定すると、
> 保存済みの入力からその年数で計算し直して並べ替えます。選択はURLに載るので共有・ブックマークできます。
> 表示される**必要年率**を必ず見てください——「3年で3倍」は年率44.2%で、実は「7年で10倍」(38.9%)より**厳しい**条件です。
>
> **「期待倍率」と「P(10倍)」は別物です。** 期待倍率は中心的な見通し、P(10倍)は
> 右裾に届く確率です。手堅い複利を求めるなら期待倍率で、テールを狙うなら P(10倍) で
> 見てください。なお現在の設定では σ(ばらつきの推定)を断面中心へ85%縮小しているため
> (要件定義書28.4)、両者の順位はかなり近くなります。これは**σ の銘柄差を主張できる
> だけのデータがまだ無い**ことの表明です。
>
> **1銘柄ずつの確率を足し算しないでください。** 10バガーの発生は共通因子(マクロ・金利・
> セクター循環)に支配されており、銘柄どうしは独立ではありません。ランキング画面の
> 「ポートフォリオとしての見通し」が、相関を織り込んだ「少なくとも1つ当たる確率」を
> 独立仮定の値と並べて表示します(要件定義書28.12)。

### データを手動で更新する

日次自動実行(後述)とは別に、手動でも実行できます。

```bash
# 収集→ゲート適用→スコアリング→前方検証→バックアップを一括実行
uv run python -m autoscreener.cli run-daily-pipeline
```

個別のステップだけ実行したい場合:

```bash
uv run python -m autoscreener.cli collect-universe        # ユニバース(候補銘柄リスト)の再取得(通常は週次で自動実行)
uv run python -m autoscreener.cli collect                 # 日次データ収集(全銘柄)
uv run python -m autoscreener.cli collect --sample 20      # 動作確認用に20銘柄だけ収集
uv run python -m autoscreener.cli collect --symbols AAPL,MSFT   # 特定銘柄だけ収集
uv run python -m autoscreener.cli backfill-history          # 価格・株式数の3年ヒストリーを一括取得(1回限りのジョブ)
uv run python -m autoscreener.cli apply-gates               # 除外ゲートを適用
uv run python -m autoscreener.cli apply-gates --date 2026-08-25   # 過去日を指定して再実行
uv run python -m autoscreener.cli run-scoring                # スコアリングエンジンを実行
uv run python -m autoscreener.cli run-scoring --date 2026-08-25   # 過去日のスコアだけ計算し直す
uv run python -m autoscreener.cli run-forward-validation      # 前方検証(実現リターン)ジョブを実行
uv run python -m autoscreener.cli estimate-elasticity          # マルチプルの成長弾力性 κ を断面から再推定
```

### 第30章「TENXの外側」の機能(EDGAR連携・マクロ・保有管理)

[`docs/outside_tenx_implementation_plan_2026-08-28.md`](docs/outside_tenx_implementation_plan_2026-08-28.md)
に基づく追加機能群。取扱可否・流動性(フェーズ1)は追加設定なしで動きますが、
SEC EDGAR連携(フェーズ2〜5)とFRED連携(フェーズ7)は `.env` の追加設定が必要です。

> ⚠ **現状(2026-08-30 実測):このグループはほぼ全部が空で回っています。**
> `.env` に `EDGAR_USER_AGENT` と `FRED_API_KEY` が設定されていないため、
> `filings` / `xbrl_facts` / `insider_transactions` / `short_interest` /
> `macro_series` / `tickers.delisted_at` はいずれも **0行** です。
> `run-daily-pipeline` はこれらの失敗を握りつぶして先へ進む設計なので
> (意図どおり)、**エラーは表に出ないまま機能だけが沈黙します**。
> UIの該当パネルは「未取得」を表示し続けます。
>
> **とくに `tickers.delisted_at` が0件であることはモデル検証の根幹に効きます。**
> [`docs/defect_and_edge_audit_2026-08-28.md`](docs/defect_and_edge_audit_2026-08-28.md) の
> D-1(擬似バックテストの母集団が100%生存者)は「致命的」と判定されたまま
> 未修正であり、`/validation` に出るKPIはすべて実態より良い方向に偏っています。
> 依存の連鎖(`EDGAR_USER_AGENT` → `refresh-cik-map` → `collect-delistings` /
> `collect-xbrl` → 生存バイアスの解消)の起点は `.env` の1行です。
> 詳細は [`docs/investment_theory_review_2026-08-30.md`](docs/investment_theory_review_2026-08-30.md) §3。

```bash
# 取扱可否(30.2.1):証券会社の取扱銘柄リストを置く。1つも無ければ全銘柄 "unknown"
mkdir -p config/tradability
echo "AAPL" > config/tradability/sbi.txt   # 1行1ティッカー、# はコメント

# 流動性・ポジション上限(30.2.2):config/portfolio.yaml を編集(既定値あり)

# EDGAR連携(30.3〜30.5、EDGAR_USER_AGENT設定後に実行)
uv run python -m autoscreener.cli refresh-cik-map     # tickers.cik を埋める(週次)
uv run python -m autoscreener.cli collect-filings     # 追跡対象銘柄のSEC提出書類を取得
uv run python -m autoscreener.cli collect-xbrl        # SEC XBRL実績値(売上・株式数・現金・負債)を取得
uv run python -m autoscreener.cli reconcile AAPL      # yfinance値とSEC原本の突合を表で確認

# 保有・モニタリング(30.7):config/positions.yaml を作成後
uv run python -m autoscreener.cli run-monitoring      # 四半期モニタリング指標・レッドフラグを評価
uv run python -m autoscreener.cli ack 123             # アラートID 123 を確認済みにする

# マクロ(30.8、FRED_API_KEY設定後)
uv run python -m autoscreener.cli collect-macro       # 米10年債利回り・実質金利・ハイイールドOASを取得
```

`run-daily-pipeline` は月曜日にこれらの週次工程(CIK突合・XBRL・マクロ)も
自動実行し、`collect-filings` と `run-monitoring` は毎日の最後に実行します
(いずれも失敗してもパイプライン全体は止まりません)。

**投資ノート**(30.7.2)は `research/<TICKER>.md` に手で書きます。雛形は
[`research/TEMPLATE.md`](research/TEMPLATE.md) を参照してください。ノートの
`thesis`・`premortem`(3件以上)・`sizing`・`verification_date` が揃うまでは
「建ててよい」状態になりません(`GET /api/v1/research/{ticker}` の
`missing_fields` で確認できます)。

### K-9:Claude API による定性分析(要約・定性評価・日次レポート)

SEC提出書類の本文をClaudeに読ませ、投資ノートの下読みに使える形にします。
`.env` に `ANTHROPIC_API_KEY` が必要です(未設定なら3コマンドとも何もせず
0件で終わります——他の機能はすべて動きます)。

> ⚠ **この3コマンドは呼ぶたびに実費が発生します。** 日次パイプラインには
> 入れておらず、人間が明示的に叩いたときだけ動きます。
> 1回あたりの銘柄数は `config/collection.yaml` の `llm.max_tickers_per_run`
> (既定25)が上限です。目安として、10-Kのリスク要因1セクション(約3万トークン)
> で入力$0.15前後です。

> **重要:これらの出力はゲートにもスコアにも入りません。** 根拠は
> [`docs/outside_tenx_implementation_plan_2026-08-28.md`](docs/outside_tenx_implementation_plan_2026-08-28.md)
> 第618行の原則1——再現性が無く検証もできない判定をブロッキング条件にしては
> ならない。LLMは同じ入力でも毎回同じ答えを返さないため、除外や順位づけの
> 根拠にするとバックテストが再現できなくなります。保存先は `llm_analyses` 表に
> 隔離してあり、`screening/` と `scoring/` がこの層を参照していないことを
> `tests/unit/test_llm_advisory_isolation.py` が検査しています。

```bash
# 直近の10-K/10-Q本文(Item 1A・Item 7)を要約 → llm_analyses.content
uv run python -m autoscreener.cli summarize-filings --symbols AAPL,MSFT

# 定性評価(構造化出力)を Batch API で作る。料金50%・最大24時間
uv run python -m autoscreener.cli score-qualitative --limit 10

# 当日ランキングの説明文を1件生成し、本文も標準出力に出す
uv run python -m autoscreener.cli generate-report --top-n 10 --show
```

同じ提出書類・同じ指示文の出力が既にあれば作り直しません(`existing` で
数えます)。`config/collection.yaml` の `llm.model` / `llm.effort` を変えると
プロンプトの指紋が変わり、新しい行として並びます(古い行は消しません——
設定変更で何がどう変わったかを後から比較するため)。

`score-qualitative` が待ち時間超過(`llm.batch_timeout_seconds`、既定24時間)で
落ちても、バッチはサーバ側で走り続けています。ログに出た `batch_id` を
`--batch-id` に渡せば、投げ直さずに回収だけをやり直せます。

生成した結果は Web UI から読めます。

| 画面 | 内容 |
| --- | --- |
| ナビの「日次レポート」(`/llm-report`) | 当日ランキングの説明文。対象日を選べます |
| 銘柄詳細ページの最下部 | その銘柄の提出書類要約と定性評価 |

**画面からは生成できません。** APIは読み取り専用である(18.6)ことに加えて、
HTTPリクエスト1本で課金が発生する導線を作らないためです——ブラウザのリロードや
監視ツールのヘルスチェックが、意図せず請求を積み上げうるので。生成はCLIだけで行い、
UIは `llm_analyses` を読むだけです(`GET /api/v1/llm/report`・`GET /api/v1/llm/{ticker}`)。

銘柄詳細で定性分析を**最下部に置いている**のも意図的です。上に置くと定量モデルの
出力より先に読まれ、順位の根拠だと受け取られます。

### モデルを検証する

```bash
# 擬似バックテスト:過去の各時点で「その時に開示済みだったデータだけ」から
# スコアを付け直し、以降の実現リターンと突き合わせる(要件定義書 27.8)
uv run python -m autoscreener.cli run-backtest

uv run python -m autoscreener.cli run-backtest --horizon-days 730   # 保有2年で検証
uv run python -m autoscreener.cli run-backtest --no-persist         # 結果を保存せず試す
```

結果は `backtest_runs` テーブルに設定スナップショットごと保存され、Web UIの
`/validation` 画面に表示されます。**パラメータを調整したら必ず再実行してください。**

`run-backtest` は KPI の算出に加えて、次の2つを学習・推定して保存します。

- **確率の較正写像** —— 「モデルの生の確率 → 実測頻度」の単調な対応表。
  `run-scoring` はこれを使って各銘柄の「1年オンペース率」を出します。
  **`config/scoring.yaml` を変更すると `config_hash` が変わり、較正写像は無効になります**
  (画面には「—」が出ます)。設定変更後は `run-backtest` → `run-scoring` の順で実行してください
- **資産相関** —— 銘柄どうしがどれだけ同時に当たる/外れるか。ランキング画面の
  ポートフォリオ確率に使われます

> ⚠ **バックテストには構造的な生存バイアスがあります。** `tickers` はNASDAQ Traderの
> **現在の**上場一覧から作られるため、期間中に上場廃止された銘柄はマスタに存在せず、
> 母集団から丸ごと欠落しています(要件定義書 27.15)。`上場廃止決済の割合` が 0.00% と
> 表示されるのはその警告灯です。リターン・オンペース率は実態より良い方向へ偏ります。

### マルチプルの成長弾力性を測り直す

```bash
uv run python -m autoscreener.cli estimate-elasticity
```

モデルは「市場は成長率が1ポイント高い企業にEV/粗利を何%高く付けているか」という
係数 κ を使って、**今の株価が既に払っている成長の対価**を差し引いています
(要件定義書28.2)。この値は**リターンにフィットさせた較正値ではなく、断面の
バリュエーション構造から測った観測値**です。

ユニバース(市場・時価総額レンジ・除外セクター)を変えたら必ず測り直し、
`config/scoring.yaml` の `multiple.growth_elasticity` を更新してください。

## 日次自動実行について

Windows Task Scheduler にタスク `AutoScreenerDailyPipeline` が登録済みで、**毎日09:00(日本時間)** に `scripts/run_daily_pipeline.bat` が自動実行されます。実行内容は `run-daily-pipeline` と同じです。

**月曜日は追加で2つ走ります。** ユニバースの再取得(週次)と、擬似バックテストの
再実行です。後者は確率の較正写像を最新の観測で学習し直すためのもので、
**スコアリングより前**に実行されます——順序が逆だと、その週のスコアは1週間古い
較正で書かれてしまいます。

- **前提**:実行時刻にPCが起動している(またはスリープ中で自動的に起こされる)こと。電源が完全に切れていると実行されません
- ログは `logs/daily_pipeline_YYYYMMDD.log` に日付ごとに保存されます
- DBバックアップは `backups/` に日付ごとに保存され、直近14日分が自動的に保持されます(gzip圧縮)
- タスクの状態確認: PowerShellで `Get-ScheduledTask -TaskName "AutoScreenerDailyPipeline"`
- プロセスが異常終了した場合は15分間隔で最大2回再試行します。既存タスクへこの設定を再適用する場合は PowerShell で `./scripts/configure_daily_pipeline_retries.ps1` を実行してください。`degraded` は監視画面/APIに保存されますが、データを重複更新し得る全パイプラインの自動再実行は行いません

### バックアップからの復元手順(四半期に1回は実際に試すこと)

`scores`・`forward_returns` は再生成不可能な検証資産(14.3)であり、バックアップが
壊れていても気づけないと「バックアップが無いのと同じ」になります。`run_backup` は
保存前に pg_dump 出力のサイズとヘッダを検証し、空/破損ダンプの保存を拒否しますが
(E-7)、**復元が実際に通るかは定期的に人手で確認する必要があります**。

1. 検証用の一時DBを作る(本番の `autoscreener` DBには触れない):
   ```bash
   docker compose exec -T db psql -U autoscreener -d postgres -c "CREATE DATABASE autoscreener_restore_test;"
   ```
2. バックアップを一時DBへ復元する:
   ```bash
   gunzip -c backups/autoscreener_YYYY-MM-DD.sql.gz | docker compose exec -T db psql -U autoscreener -d autoscreener_restore_test
   ```
3. 主要テーブルの件数が妥当か確認する:
   ```bash
   docker compose exec -T db psql -U autoscreener -d autoscreener_restore_test \
     -c "SELECT (SELECT count(*) FROM scores) AS scores, (SELECT count(*) FROM forward_returns) AS forward_returns;"
   ```
4. 確認できたら一時DBを削除する:
   ```bash
   docker compose exec -T db psql -U autoscreener -d postgres -c "DROP DATABASE autoscreener_restore_test;"
   ```
5. 最低でも四半期に1回、この手順を実際に実行する。

## TENX v2 Live Investment Intelligence

Live Intelligence は既存の `P(horizon年でtarget_moic倍)` を変更しない表示・履歴層です。
Consensus、Guidance、KPI、債務、資本配分、TAM、マイルストーン等には必ず取得時点と
`coverage_status` を残し、`not_collected` と `collected_no_finding` を区別します。

初回はマイグレーション後、次の順で収集します。

```bash
uv run alembic upgrade head
uv run python -m autoscreener.cli collect-filings
uv run python -m autoscreener.cli collect-filing-sections
uv run python -m autoscreener.cli collect-guidance
uv run python -m autoscreener.cli collect-consensus
uv run python -m autoscreener.cli collect-investment-intelligence
```

`collect-investment-intelligence` は保存済みSEC本文だけを読み、KPI・債務満期・資本配分・
DEF 14Aの所有情報と、研究ノートの `milestones` を再実行可能な形で保存します。
日次パイプラインではこれらを提出書類収集後に自動実行します。Consensus の外部取得失敗は
Coreスコアを止めず `collection_failed` として履歴化します。

画面は銘柄詳細の意思決定順セクションと `/data-coverage` で確認できます。すべての
Live Intelligence API は `as_of` を受け取り、その日より後に観測された値を返しません。
`/validation` が FAIL / STALE の間、ランキング上部には `Research Only` が固定表示されます。

## 設定のカスタマイズ

`config/` 配下のYAMLファイルを編集することで、コードを変更せずに閾値や重みを調整できます。

| ファイル | 内容 |
|---|---|
| `config/universe.yaml` | 時価総額・売上高の上限、株価下限、流動性下限、除外セクターなど |
| `config/collection.yaml` | 収集の並列度・リトライ・サーキットブレーカーの閾値 |
| `config/scoring.yaml` | 実現倍率モデルの全パラメータ(成長の減衰と品質による調整・価格ナウキャスト・粗利率の外挿・マルチプルの成長弾力性・生存確率・不確実性とσの縮小・最低要件・較正)。`scoring_version` を変えると新旧スコアが別系列として共存する |
| `config/portfolio.yaml` | ポジションサイジングの規律(30.2.2/30.7):総投資予定額・1銘柄上限・ADV参加率上限・セクター上限 |
| `config/monitoring.yaml` | 保有銘柄の四半期モニタリング閾値(30.7.3)。閾値は売却条件ではなく「判断をやり直す合図」 |
| `config/tradability/*.txt` | 証券会社ごとの取扱銘柄リスト(30.2.1)。利用者が手動更新。1つも無くてもエラーにはならない |
| `config/positions.yaml` | 保有銘柄(30.7.1)。アプリは読むだけで書き込まない。無くても正常(保有なし) |
| `research/<TICKER>.md` | 投資ノート(30.7.2)。雛形は `research/TEMPLATE.md` |

> `config/scoring.yaml` のパラメータには**性質の異なる3種類**があり、
> ファイル内のコメントで区別してあります(要件定義書28章)。
>
> | 種類 | 例 | 扱い |
> |---|---|---|
> | 断面から測った構造パラメータ | `multiple.growth_elasticity`(κ) | `estimate-elasticity` で測り直す。リターンにフィットさせない |
> | 公表された基準率からの事前値 | `survival.*`、`uncertainty.*_sigma` | 据え置き |
> | 擬似バックテストで選んだ値 | `growth.nowcast_*`、`uncertainty.sigma_shrinkage` | **弱くしか特定されていない** |
>
> 3つめは独立な観測期間が実質3つ程度しかない標本で選んでいるため、
> さらに細かく合わせ込むのは過学習になります(要件定義書 27.18)。
> 変更したら必ず `run-backtest` でKPIの変化を確認してください。

設定ファイルはPydanticで検証されており、不正な値(下限が上限を上回る、確率でない値を確率欄に入れる等)を入れると起動時にエラーになります。

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `設定ファイルとAPIのコードのスキーマが一致していません` / `N validation errors for ScoringConfig` | **APIプロセスの再起動忘れ**です。古いコードが新しい `config/*.yaml` を読むと、そのコードが要求する(新しい設定には存在しない)フィールドが「Field required」として並びます。**設定ファイルではなくプロセスが古い**のが原因なので、設定を書き戻してはいけません。APIを再起動してください |
| 再起動したのに古い応答が返ってくる | **同じポートを複数のプロセスがLISTENしている**可能性があります(Windowsでは複数プロセスが同一ポートにbindできてしまい、リクエストがどちらに振られるか不定になります)。`netstat -ano \| findstr :8000` で確認し、出てきたPIDを**すべて**停止してから起動し直してください。`--reload` 付きで起動している場合、停止すべきプロセスはリローダー親とワーカー子の**2つ**あります |
| `/ready` が200なのに画面が動かない | v4以降はこの状態にならないようにしてあります(`/ready` はDBだけでなく設定ファイルの検証も行い、噛み合っていなければ503を返します)。それでも起きる場合はブラウザではなく `curl` で直接叩き、`/ready` の `scoring_version` が期待どおりかを確認してください |
| 全画面に「Failed to fetch」が出る | ほぼ必ず**APIプロセスの再起動忘れ**です。`http://localhost:8000/ready` を開いて確認してください(`/health` はDBを見ないので、スキーマが古くても200を返します)。マイグレーション後は必ずAPIを再起動してください。`--reload` 付きで起動していれば自動的に読み直されます |
| `docker compose up -d --wait` がタイムアウトする | Docker Desktopが起動しているか確認 |
| 収集中に`HTTP Error 401/429`が多発する | Yahoo Finance側のレート制限。短時間に何度も全銘柄収集を実行すると起きやすい。自動的にリトライ・隔離される設計だが、頻発する場合は時間を空けて再実行する。秒あたりの上限は `config/collection.yaml` の `yfinance_requests_per_second`(既定2.0)で下げられます |
| EDGARが403や429を返す | SECの制限は**IP単位**です。`config/collection.yaml` の `edgar.requests_per_second`(既定5.0、SECの上限は約10)を下げてください。403/429を受けると `edgar.throttle_cooldown_seconds`(既定60秒)だけSEC向けリクエスト全体が自動で止まります。**同じマシンで日次パイプラインとテストスイートを同時に走らせない**こと——レートは単純に足し算になります |
| APIから`http://localhost:5173`のCORSエラーが出る | `autoscreener/api/main.py`のCORS許可オリジンを確認(既定は`localhost:5173`のみ許可) |
| スコアが表示されない・空になる | `collect` → `apply-gates` → `run-scoring` の順で実行されているか確認。`GET /api/v1/universe/status` で直近の実行状況を確認できる |
| 監視リストの「見通しがマイナス」から銘柄を開くとエラーになる | v4で修正済みです(要件定義書 28.19②)。それでも起きる場合はAPIプロセスを再起動してください |
| 「1年オンペース率」が全銘柄「—」になる | 較正写像が無い状態です。`config/scoring.yaml` を変更したあとに `run-backtest` を実行していないと起きます(較正写像は設定のハッシュが完全一致するときだけ使われます)。`run-backtest` → `run-scoring` の順で実行してください |
| 上位銘柄の「1年オンペース率」が全部同じ値になる | 仕様です。較正は観測した確率帯の外へ外挿しないため、最上位の帯より高い予測にはその帯の実測頻度がそのまま当てられます。順位は `P(10倍)` 側で保たれています(要件定義書 28.8) |
| ゲート通過数に比べてランキング件数が少ない | 仕様です。モデルの必須入力が揃わない銘柄と、期待倍率が1.0を下回る(中心的な見通しで株主価値を毀損する)銘柄には順位を付けず、監視リストへ回しています(要件定義書 27.17) |
| バックテストで「観測が0件」になる | `price_snapshots` の期間がホライズンより短い。`backfill-history` を実行するか `--horizon-days` を短くしてください |
| `summarize-filings` / `score-qualitative` / `generate-report` が「0件」で静かに終わる | `.env` の `ANTHROPIC_API_KEY` が未設定です(FRED と同じく、未設定は失敗ではなく「使わない構成」として扱います)。`config/collection.yaml` の `llm.enabled: false` でも同じ挙動になります |
| `refresh-cik-map` / `collect-filings` / `collect-xbrl` が `ValueError: EDGAR_USER_AGENT が未設定です` で落ちる | `.env` に `EDGAR_USER_AGENT`(連絡先メールアドレスを含む文字列)を設定してください。SECの利用規約で必須です(30.3.1) |
| `GET /macro` が常に `enabled: false` を返す | `.env` に `FRED_API_KEY` が未設定です。任意機能なので未設定でも他は動きますが、マクロ画面を使うには設定してください(30.8.1) |
| 銘柄詳細の「提出書類とレッドフラグ」が常に「未確認」 | その銘柄が追跡対象(保有銘柄・ランキング上位N件・投資ノートのある銘柄の和集合、30.3.4)に入っていないか、`collect-filings` をまだ実行していません |

## テスト

```bash
uv run pytest                 # バックエンド(Postgresが起動している必要あり)
cd frontend && npm run build   # フロントエンドの型チェック・ビルド確認
```
