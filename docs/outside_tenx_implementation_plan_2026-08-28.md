# 第30章 「TENXの外側」の実装計画(2026-08-28)

> **この文書の目的**:アーティファクト「TENXの外側」(投資判断に必要だがアプリが答えていない情報の作業台帳)を、**このリポジトリで実装可能な差分**に翻訳したもの。実装担当がこの文書だけを読んで着手できるよう、テーブル定義・関数シグネチャ・APIの契約・テスト名・受け入れ基準まで確定させてある。
>
> 出典アーティファクト:`https://claude.ai/code/artifact/1a961b77-29b9-4227-bba6-c01b19aad936`(「TENXの外側」2026-08-27)
> 関連文書:`10bagger_app_requirements.md`(第1〜29章)、`model_audit_v4_2026-08-26.md`、`defect_audit_2026-08-27.md`
>
> 本文書内の節番号 `30.x` は、コードコメントからの参照キーとして使う(既存コードが `27.15` や `14.3` を引いているのと同じ規約)。

---

## 30.0 実装担当者への総則

### 30.0.1 着手前に必ず読むもの

| 対象 | なぜ |
|---|---|
| `10bagger_app_requirements.md` の 14章(考慮不足の設計要件)・18章(例外処理)・27章・28章 | 本計画の判断はすべてこの章の制約の上に立っている。特に **14.3(ポイントインタイム/先読みバイアス)** と **18.6(APIは読み取り専用DBロール)** は、本計画の設計を左右した2大制約である |
| `src/autoscreener/collectors/yfinance_client.py` | 外部APIクライアントの既存作法(リトライ・エラー分類・空応答の扱い)。EDGARクライアントはこの構造を**踏襲する**、新しく発明しない |
| `src/autoscreener/screening/watchlist.py` | 「純粋関数として実装し、DBアクセスは呼び出し元(API層/バッチ層)が持つ」という層分けの手本。本計画で追加する判定ロジックはすべてこの形にする |
| `src/autoscreener/api/routes.py` の `_compute_warnings` | 警告バッジの追加方法。フロント側は `frontend/src/warnings.ts` と `frontend/src/glossary.ts` に対応する説明を必ず足す |
| `src/autoscreener/batch/daily_pipeline.py` | 新しいバッチ工程を差し込む場所と、失敗を握りつぶしてよい工程/よくない工程の判断基準 |

### 30.0.2 守るべきコーディング規約(このリポジトリ固有)

1. **コメントは日本語で、「何をしているか」ではなく「なぜそうしたか」を書く。** ある選択肢を**採らなかった**理由は特に必ず残す。既存コードはすべてこの形式で書かれている。
2. **要件番号を引用する。** 新規コードは本文書の `30.x` を、既存の判断に依拠する箇所は元の章番号を引く。
3. **設定値はハードコードせず Pydantic モデルに載せる**(`src/autoscreener/config.py`)。妥当性は `model_validator` で検証し、壊れた設定はプロセス起動時に落とす(18章 fail-fast)。
4. **純粋関数とI/Oを分ける。** テストは `tests/unit/` に置き、DBもネットワークも触らない形で書けるようにする。ネットワークを触るテストは `responses`(dev依存に既にある)でスタブする。
5. **マイグレーションは `uv run alembic revision --autogenerate -m "..."` で雛形を作り、生成物を必ず目視で直す。** 既存の `alembic/versions/*.py` と同じ体裁を保つ。
6. **README を更新する。** 新しいCLIコマンド・新しい設定ファイル・新しい初回セットアップ手順が増えたら `README.md` の該当節に追記する。
7. **秘密や個人情報を config/*.yaml に書かない。** EDGARが要求する連絡先メールアドレスは `.env`(`Settings`)に置く。`.env.example` にはプレースホルダを書く。

### 30.0.3 全体像(依存関係と実装順序)

```
フェーズ1  取扱可否・流動性・ポジション上限        … 既存データのみ。他に依存しない
   │
フェーズ2  EDGAR連携基盤(CIK解決・クライアント・filings)
   │            │
   │            ├─ フェーズ3  レッドフラグ判定(即死要因の自動検出)
   │            ├─ フェーズ4  XBRL突合(yfinance値の検算)
   │            └─ フェーズ5  将来の希薄化(シェルフ/ATM/転換社債)
   │
フェーズ6  保有・投資ノート・四半期モニタリング   … フェーズ1・3に依存
フェーズ7  マクロ(FRED)                          … 独立。いつでも着手可
フェーズ8  フロントエンド統合                      … 1〜7の成果を1画面にまとめる
```

**フェーズ1だけでも単体で価値が出る**(元文書の実務ワークフロー工程1〜2をそのまま消す)。フェーズ2は基盤なので単体では価値が出ないが、3・4・5がすべてその上に載る。**上から順に実装し、各フェーズの受け入れ基準を満たしてから次へ進むこと。** フェーズをまたいで並行着手しない(DBマイグレーションが競合する)。

---

## 30.1 スコープ判断 —— 何を実装し、何を実装しないか

### 30.1.1 判断の原則

出典アーティファクトは「アプリの外側の作業」を列挙した文書であり、**その全部をアプリの内側に取り込もうとするのは誤りである**。取り込むかどうかは次の3原則で決めた。

**原則1:機械が測れるものだけを機械に測らせる。定性判断はアプリが代行せず、記録の器だけを用意する。**

TAM のボトムアップ再構築、モートの型の特定、経営者の経歴の評価、ショートレポートの証拠の吟味——これらを自動化すると、**必ず「それらしい答え」が出て、人間が確認したという錯覚を生む**。10バガー探索でこの錯覚は致命的で、モデルが既に持っている「ランキング上位という事実だけでは根拠にならない」(27章・14.2)という自己認識を裏切る。よって**判断は人間が行い、アプリはその結論を構造化して保存し、記入漏れを指摘する**にとどめる(元文書 第13節・工程9「埋められない項目があるうちは建てない」をそのまま実装する)。

**原則2:利用者が書くデータはファイル(git管理)、機械が導くデータはDB。**

理由が2つある。

- **18.6:APIレイヤーは読み取り専用DBロールで動く。** 画面から書き込む機能を足すには書き込み経路と認証を作ることになり、個人利用(11.1解釈A)には過剰。
- **元文書 第13節が「後から書き換えないこと」を記録の要件として挙げている。** git のコミット履歴はこの要件をそのまま満たす。DBの行を UPDATE する設計は、逆にこの要件を破りやすい。

したがって、**保有銘柄は `config/positions.yaml`、投資ノートは `research/<TICKER>.md`** とし、アプリは読むだけにする。

**原則3:EDGAR由来のシグナルを除外ゲート(`evaluate_gates`)に入れてはならない。**

`apply_gates` の出力(`universe_snapshots`)は、擬似バックテスト(27.8)が過去日の母集団を再構成するときの定義そのものである。ファイリング由来のフラグは**提出日が分かるのでそれ自体はポイントインタイム安全**だが、収集対象が「今日のマスタに存在する銘柄」に限られる以上、過去に遡って適用すると**生存バイアス(27.15 / 残課題R-1)を別経路から増やす**。よってレッドフラグは `screening/red_flags.py` という**独立した表示・アラート層**に置き、ゲート判定には一切触れない。この境界を破ると、モデルの検証資産が静かに壊れる。

### 30.1.2 出典アーティファクトの各節に対する処遇

| 節 | 内容 | 処遇 | 実装先 |
|---|---|---|---|
| 00 | 4因子と外部検証の対応 | **実装**(XBRL突合) | フェーズ4 |
| 01 | SEC EDGAR 一次情報 | **実装**(取得基盤+レッドフラグ) | フェーズ2・3 |
| 02 | 決算説明会・IR資料 | 対象外(詳細画面にリンクを出すのみ) | フェーズ8 |
| 03 | TAM・ユニットエコノミクス・顧客集中・モート | **記録の器のみ**(ノートのテンプレート項目) | フェーズ6 |
| 04 | 経営者と資本配分 | 部分実装(424B5の時系列は自動、評価は人間) | フェーズ5 |
| 05 | 生存とダウンサイド | **実装**(going concern / 内部統制 / NT / 上場基準) | フェーズ3 |
| 06 | 需給と市場構造 | **実装**(ADV・ポジション上限)/ 空売り残高は対象外 | フェーズ1 |
| 07 | 触媒とカレンダー | 部分実装(次回決算日と検証日の管理) | フェーズ6 |
| 08 | 反対意見の能動収集 | **記録の器のみ**(プレモーテム3項目+先行指標) | フェーズ6 |
| 09 | マクロとレジーム | **実装**(FRED 3系列) | フェーズ7 |
| 10 | 執行・税・為替 | **取扱可否のみ実装**。税務・為替は対象外 | フェーズ1 |
| 11 | ポートフォリオ設計 | **実装**(サイズ上限の算出。判断は人間) | フェーズ1・6 |
| 12 | 保有後のモニタリング | **実装**(上4行は既存データ、残りは filings) | フェーズ6 |
| 13 | 記録 | **実装**(ノートのスキーマ検証と記入漏れ検出) | フェーズ6 |
| 14 | 情報源カタログ | 対象外(この文書と用語集で足りる) | — |
| 15 | 実務ワークフロー | **実装**(工程1〜5をチェックリストとして提示) | フェーズ8 |
| 16 | 法務上の注意 | フロントの免責表示に反映 | フェーズ8 |

### 30.1.3 明確に実装しないもの(と、その理由)

- **空売り残高・貸株コスト**:無料で取れるのは月2回・数営業日遅れのFINRAデータだけで、日次バッチに載せても情報が古い。判断に使うには有料データ(Ortex/S3)が要り、費用対効果が見合わない。元文書が言うとおり「なぜ売られているか」を人間が調べるほうが本質なので、ノートの項目に留める。
- **決算説明会トランスクリプトの自動取得・要約**:安定した無料の機械取得先が無く、規約上の懸念もある。要約を生成すると原則1に反する(読んだつもりになる)。
- **税務・為替の計算**:制度改正に追随できないものを実装すると、古い前提で誤った金額を表示することになる。これは何も表示しないより悪い。
- **ショートレポート・訴訟記録のスクレイピング**:出典が分散し、規約が各社異なる。詳細画面に**検索リンク**を出すだけにする(人間が2クリックで到達できれば工程としては足りる)。
- **上場廃止銘柄の履歴データの購入**(残課題R-1の本丸):実装の問題ではなく調達の問題であり、本計画の対象外。ただしフェーズ2で `tickers.cik` が入ると、**Form 25-NSE / 15-12B から上場廃止イベントを拾える道が開く**(将来の別計画として 30.10 に記す)。

---

## 30.2 フェーズ1:安いフィルタの自動化

> **狙い**:元文書の実務ワークフロー工程1(取扱可否・2分)と工程2(流動性・2分)を、人間の作業から消す。**外部APIを一切増やさずに実装できる**ので最初に着手する。

### 30.2.1 取扱可否(tradability)

#### 設計判断:なぜ証券会社のサイトを叩かないのか

日本のネット証券は取扱銘柄一覧のAPIを公開していない。スクレイピングは規約上も安定性上も採れない。よって**利用者が証券会社サイトから取得した銘柄リストをファイルとして置き、アプリはそれを読む**。手動更新になるが、取扱銘柄は日々変わるものではないので月1回の更新で足りる。

**重要な設計方針:リストに無い銘柄を「取扱不可」と断定しない。** リストが古い/不完全な可能性が常にあるため状態は3値(`tradable` / `not_listed` / `unknown`)とし、**リストファイルが1つも無いときは全銘柄 `unknown`** とする。「データが無い」を「不可」と表示するのは、このリポジトリが繰り返し戒めてきた誤り(27.17・A-1と同型)である。

#### 実装

**新規ディレクトリ**:`config/tradability/`(`.gitignore` に入れない。銘柄リストは秘密ではない)

ファイル形式は1行1ティッカーのプレーンテキスト。ファイル名(拡張子を除く)が証券会社名になる。

```
config/tradability/sbi.txt
config/tradability/rakuten.txt
config/tradability/ibkr.txt
```

```text
# 行頭 # はコメント。空行は無視。
# 2026-08-28 SBI証券 米国株取扱銘柄一覧より作成
AAPL
ABCD
```

**新規モジュール**:`src/autoscreener/screening/tradability.py`

```python
"""証券口座で発注できるかの判定(30.2.1)。

元文書の実務ワークフロー工程1。**デューデリの最初の工程**であり、ここで
落ちる銘柄に分析時間を使わないことがワークフロー全体の設計意図である。

リストに無いことを「取扱不可」と断定しないのは、リストが利用者の手動更新に
依存していて古くなりうるため。`unknown` と `not_listed` を分けておかないと、
更新を忘れた月に全銘柄が「買えない」と表示され、機能そのものが信用を失う。
"""

TRADABLE = "tradable"
NOT_LISTED = "not_listed"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class BrokerCoverage:
    """1証券会社の取扱銘柄集合。"""

    broker: str
    symbols: frozenset[str]
    source_path: Path
    loaded_at: datetime.date  # ファイルの mtime。古さをUIに出すため


@dataclass(frozen=True)
class TradabilityResult:
    status: str          # TRADABLE / NOT_LISTED / UNKNOWN
    brokers: list[str]   # 取扱のある証券会社名(status=TRADABLE のときのみ非空)


def load_broker_coverage(directory: Path | None = None) -> list[BrokerCoverage]:
    """`config/tradability/*.txt` をすべて読む。ディレクトリが無ければ空リスト。"""


def evaluate_tradability(symbol: str, coverage: list[BrokerCoverage]) -> TradabilityResult:
    """純粋関数。coverage が空なら必ず UNKNOWN を返す。"""
```

- **正規化**:比較前に `symbol.strip().upper()` し、`.` と `-` の表記ゆれ(`BRK.B` / `BRK-B`)を吸収する。両方を集合に入れる `_normalize_variants(symbol) -> set[str]` を用意すること。
- **キャッシュ**:API層は毎リクエストでファイルを読まない。**mtime を見て変わっていたら読み直す**小さなローダを書く(開発中にファイルを差し替えても再起動不要にするため。`functools.lru_cache` だと差し替えが効かない)。

#### API変更

`CandidateSummary` / `CandidateDetail`(`src/autoscreener/api/schemas.py`)に追加:

```python
    # 30.2.1:証券口座で発注できるか。"tradable" / "not_listed" / "unknown"。
    # リストファイルが無いときは全銘柄 "unknown"(不可と断定しない)。
    tradability: str = "unknown"
    tradable_brokers: list[str] = []
```

`GET /candidates` に新規クエリパラメータ `tradable_only: bool = False` を追加。`True` のとき `status == TRADABLE` の銘柄だけを返す。**既定を False にするのは、リスト未整備の利用者に空の画面を見せないため。**

### 30.2.2 流動性とポジション上限

**新規モジュール**:`src/autoscreener/screening/liquidity.py`

```python
"""流動性とポジションサイズ上限(30.2.2)。

元文書 第06節・第11節。ポジションサイズの上限を決めるのはモデルの確信度では
なく板の厚さである、という原則を数字にする。**新しいデータ取得は不要**で、
既存の `price_snapshots`(14.11で正規化済みのOHLCV)だけで計算できる。
"""

ADV_WINDOW_DAYS = 20  # 元文書 第06節「20日平均売買代金」
MIN_OBSERVATION_DAYS = 5  # これ未満は平均と呼べないので None を返す


@dataclass(frozen=True)
class LiquidityProfile:
    adv_usd: float | None                     # 20営業日平均売買代金(終値 × 出来高)
    observation_days: int                     # 実際に使えた営業日数。20未満なら参考値
    max_position_adv_usd: float | None        # ADV × adv_participation_cap
    max_position_portfolio_usd: float | None  # 総資産 × per_position_cap
    max_position_usd: float | None            # 上2つの小さいほう(元文書 第11節)
    binding_constraint: str | None            # "liquidity" / "portfolio" / None


def compute_liquidity_profile(
    closes_and_volumes: list[tuple[float | None, int | None]],
    *,
    portfolio_value_usd: float | None,
    adv_participation_cap: float,
    per_position_cap: float,
) -> LiquidityProfile:
    """純粋関数。DBには触らない。呼び出し元が直近20営業日ぶんを渡す。"""
```

- `observation_days < ADV_WINDOW_DAYS` でも計算はするが、**その事実を必ず返す**(UIが「参考値」と出せるように)。`observation_days < MIN_OBSERVATION_DAYS` なら `adv_usd = None`。
- 終値・出来高いずれかが `None` の日は分母から除く。
- **どちらの制約が効いているか(`binding_constraint`)を返すのが要点。** 「上限は $4,000」とだけ出すと、板が薄いのか規律が効いているのか分からず、利用者は前者のとき分割発注という手が取れることに気づけない。

#### 設定追加

**新規ファイル**:`config/portfolio.yaml`

```yaml
# 30.2.2 / 30.7:ポジションサイジングの規律。
# 元文書 第11節「等金額・銘柄数を多く・上限を固定・小さいほうを採る」に対応する。
# **この値はモデルが決めるものではなく利用者が決めるもの**である。ケリー基準的な
# 最適化を行わないのは、確率の絶対水準がまだ検証途上だから(27章・14.2)。
portfolio_value_usd: 100000.0    # 総投資予定額(USD)。サイズ上限の分母
per_position_cap: 0.04           # 1銘柄あたりの上限(総資産比)。元文書の推奨は3〜5%
binary_event_position_cap: 0.02  # 二値イベント(FDA承認等)を抱える銘柄の上限。通常の半分
adv_participation_cap: 0.10      # 1銘柄のポジションがADVに占める上限。元文書 第06節
sector_cap: 0.25                 # 1セクターあたりの上限(フェーズ6の集計で使う)
max_positions: 30                # 銘柄数の上限。等金額運用の分母の目安
```

`src/autoscreener/config.py` に `PortfolioConfig(BaseModel)` と `load_portfolio_config()` を追加。`model_validator` で次を検証する:`0 < per_position_cap <= 1`、`binary_event_position_cap <= per_position_cap`、`0 < adv_participation_cap <= 1`、`sector_cap >= per_position_cap`。

#### API変更

`CandidateSummary` / `CandidateDetail` に追加:

```python
    # 30.2.2:20日平均売買代金と、そこから決まる1銘柄あたりの投入上限。
    adv_usd: float | None = None
    adv_observation_days: int | None = None
    max_position_usd: float | None = None
    # "liquidity"(板が制約) / "portfolio"(規律が制約)。どちらが効いているかを見せる
    position_binding_constraint: str | None = None
```

**N+1を作らないこと。** `list_candidates` は1クエリで対象ティッカーの直近20営業日分をまとめて取る。既存の `_latest_raw_snapshots_by_ticker`(`DISTINCT ON` を使う)が手本。目安:

```sql
SELECT ticker_id, trade_date, close, volume
FROM price_snapshots
WHERE ticker_id = ANY(:ids) AND trade_date > :cutoff
ORDER BY ticker_id, trade_date DESC
```

`cutoff` は「基準日 − 40暦日」(20営業日を確実に含む)。Python側でティッカーごとに新しい順で先頭20件を取る。

### 30.2.3 フロントエンド(フェーズ1分)

- `frontend/src/pages/RankingPage.tsx`:列を2つ追加。**取扱可否**はバッジ(`tradable`=実線、`not_listed`=淡色、`unknown`=枠線のみ)、**投入上限**は金額と制約種別。`tradable_only` のトグルを絞り込み欄に置く。
- `frontend/src/api/types.ts` / `client.ts`:新フィールドと新パラメータを反映。
- `frontend/src/glossary.ts`:`adv`(平均売買代金)、`max_position`(投入上限)、`tradability`(取扱可否)の3項目を追加。カテゴリはそれぞれ `portfolio` / `portfolio` / `app`。**`short` に別の専門用語を入れない**という既存方針を守る。

### 30.2.4 フェーズ1の受け入れ基準

- [ ] `config/tradability/` が空(またはディレクトリごと無い)状態でランキングが従来どおり表示され、全銘柄が `unknown` になる
- [ ] 1銘柄だけ書いたリストを置くと、その銘柄だけ `tradable` になり `tradable_brokers` に証券会社名が入る
- [ ] `BRK.B` を書いたリストで `BRK-B` が `tradable` と判定される(表記ゆれ吸収)
- [ ] `adv_usd` が20営業日ぶんの `close × volume` の単純平均と一致する(1銘柄を手計算で検算)
- [ ] `max_position_usd` が「ADV×10%」と「総資産×4%」の小さいほうになり、`position_binding_constraint` が正しい
- [ ] `price_snapshots` が5営業日未満しか無い銘柄で `adv_usd` が `None` になり、APIが500を返さない
- [ ] 新規テスト:`tests/unit/test_tradability.py`、`tests/unit/test_liquidity.py`
- [ ] `list_candidates` の `price_snapshots` へのクエリが1リクエストあたり1回に収まっている

---

## 30.3 フェーズ2:EDGAR連携基盤

> **狙い**:一次情報を機械可読で取る土台を作る。**このフェーズ単体ではUIに何も出ない**。出さないことを許容する代わりに、フェーズ3〜5がすべてこの上に載る。

### 30.3.1 SECのアクセス規約(守らないとIP単位で遮断される)

| 規約 | 実装での担保 |
|---|---|
| `User-Agent` に**組織名/氏名と連絡先メールアドレス**を明示する | `Settings.edgar_user_agent`(`.env` から読む)。未設定ならクライアント生成時に即例外(fail-fast)。**空文字や既定値のまま本番アクセスさせない** |
| リクエストは概ね **10 req/s 以下** | `_RateLimiter`(既定 8.0 req/s)。余裕を持たせるのは、並列実行時にバーストが乗るため |
| `Accept-Encoding: gzip, deflate` を送る | 既定ヘッダに含める |
| 大量取得は業務時間外に | 日次パイプラインの実行時刻に依存するため、**利用者への注意としてREADMEに書く**にとどめる |

**エンドポイント一覧**(すべてGET・無認証):

| 用途 | URL |
|---|---|
| ティッカー→CIK対応表 | `https://www.sec.gov/files/company_tickers.json` |
| 提出書類一覧 | `https://data.sec.gov/submissions/CIK{cik:010d}.json` |
| XBRL全社実績 | `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json` |
| XBRL単一タグ | `https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json` |
| 提出書類の本文 | `https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_nodash}/{primary_document}` |

`submissions` の `filings.recent` は**列指向の並列配列**である(`accessionNumber` / `filingDate` / `reportDate` / `form` / `items` / `primaryDocument` などが同じ長さの配列で並ぶ)。古い分は `filings.files[]` に別JSONへのポインタが入るが、**本計画では `recent` だけを使う**(直近1000件・約1年分あれば十分で、全履歴を取ると1銘柄あたりの転送量が跳ねる)。

### 30.3.2 CIK の解決

**マイグレーション**:`tickers` に列を1つ追加。

```python
    # 30.3.2:SECの企業識別子(10桁ゼロ埋め文字列)。EDGARのあらゆるAPIの鍵になる。
    # 文字列にするのは、ゼロ埋めの桁数がURLの仕様そのものであり、intにすると
    # 呼び出し側が毎回 f"{cik:010d}" を書くことになるため。
    cik: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
```

**新規バッチ**:`src/autoscreener/batch/refresh_cik_map.py`

```python
def refresh_cik_map() -> dict[str, int]:
    """company_tickers.json を取得し、`tickers.cik` を埋める。

    戻り値は {"matched": n, "unmatched": n, "updated": n}。
    週次(月曜・ユニバース再取得と同じ日)に実行する。
    """
```

- **突合はシンボル文字列で行う。** SECは `BRK-B` 形式、NASDAQ Trader は `BRK.B` 形式のことがあるため、`tradability` と同じ `_normalize_variants` 相当の正規化を共通化して使う(`src/autoscreener/symbols.py` に切り出し、`tradability.py` からも参照する)。
- **1シンボルが複数CIKに当たる場合は埋めない**(曖昧なまま埋めると別会社の書類を読むことになる)。`unmatched` に数え、ログにWARNINGを出す。
- CLI:`tenx refresh-cik-map`(`src/autoscreener/cli.py` にコマンド追加)。

### 30.3.3 EDGARクライアント

**新規モジュール**:`src/autoscreener/collectors/edgar_client.py`

```python
"""SEC EDGAR API の薄いラッパー(30.3)。

`yfinance_client.py` と同じ構造にしてある——リトライは tenacity、失敗は
`collectors/errors.py` の `CollectionError` 階層に分類、レート制御は
モジュール内で完結。**yfinanceと違い、SECは規約違反に対してIP単位の遮断で
応じる**ため、レート制御はベストエフォートではなく必須の要件として扱う。
"""


class RateLimiter:
    """トークンバケットではなく「最小間隔」方式。

    バケット方式にしないのは、蓄積したトークンでバーストが起きうるため。
    SECの制限は瞬間レートに対するものなので、間隔を一定に保つほうが安全側。
    `threading.Lock` で保護し、`time.monotonic()` を使う(システム時刻の
    変更に影響されないため)。
    """

    def __init__(self, requests_per_second: float) -> None: ...
    def acquire(self) -> None: ...


@dataclass(frozen=True)
class FilingRecord:
    """`submissions` の1件分。DBの `filings` 行にそのまま対応する。"""

    accession_number: str      # "0001234567-25-000123"
    form: str                  # "8-K" / "NT 10-Q" / "424B5" ...
    filed_date: datetime.date
    report_date: datetime.date | None
    items: list[str]           # 8-Kのアイテム番号(例 ["2.02", "9.01"])。他フォームは空
    primary_document: str | None
    document_url: str | None


class EdgarClient:
    def __init__(self, config: EdgarConfig, user_agent: str) -> None:
        """user_agent が空・None・プレースホルダのままなら ValueError(fail-fast)。"""

    def fetch_company_tickers(self) -> dict[str, str]:
        """{正規化シンボル: 10桁CIK}。"""

    def fetch_filings(self, cik: str, *, forms: set[str] | None = None) -> list[FilingRecord]:
        """`filings.recent` を FilingRecord に整形する。forms 指定で絞り込む。"""

    def fetch_company_facts(self, cik: str) -> dict:
        """companyfacts の生JSON。フェーズ4で使う。"""

    def fetch_document_text(self, url: str, *, max_bytes: int = 8_000_000) -> str:
        """提出書類本文をプレーンテキストにして返す(lxmlでタグを除去)。

        `max_bytes` を設けるのは、10-Kが数十MBになる銘柄が実在し、
        全銘柄ぶんをメモリに載せると日次バッチが落ちるため。
        超過分は切り捨てる——going concern の記載は監査報告書と
        流動性の節にあり、文書前半〜中盤に現れるので実務上支障がない。
        """
```

**`items` のパース**:`submissions` の `items` は `"2.02,9.01"` のようなカンマ区切りのこともあれば、`"Item 2.02: Results of Operations and Financial Condition"` のような説明つきのこともある。**正規表現 `\b(\d\.\d{2})\b` で番号だけを抜き出す**こと(フォーマットの揺れをここで吸収し、下流には番号の配列だけを渡す)。

**エラー分類**:`collectors/errors.py` の既存分類をそのまま使う。追加の判断は次の2点のみ。

- HTTP 403 は**規約違反(User-Agent不備・レート超過)を強く示唆する**ので `TransientFailure` ではなく **`PermanentFailure` として扱い、ログにERRORを出す**。リトライで叩き続けると遮断が長引く。
- HTTP 404 は `EmptyResponseError`(CIKはあるが提出が無い/新規上場直後)。

**設定追加**:`config/collection.yaml` に新セクション。

```yaml
# 30.3:SEC EDGAR。`user_agent` は .env の EDGAR_USER_AGENT から読む
#(連絡先メールアドレスを含むためgit管理下に置かない)。
edgar:
  enabled: true
  requests_per_second: 8.0   # SECの上限は約10。並列時のバーストを見込んで余裕を取る
  timeout_seconds: 30.0
  # 本文取得(going concern等の検出)を行うか。1銘柄あたり数MBの転送になるため、
  # 追跡対象銘柄(30.3.4)に限って実行する。
  document_fetch_enabled: true
  # 日次で提出一覧を取りに行く銘柄数の上限。ユニバース全体(数千件)を毎日
  # 舐めるのはSECにもこちらにも過大なので、追跡対象だけに絞る。
  max_tracked_tickers: 300
  retry:
    max_attempts: 3
    backoff_base_seconds: 1.0
    backoff_max_seconds: 30.0
```

`Settings`(`config.py`)に追加:

```python
    # 30.3.1:SECが要求する連絡先つき User-Agent。
    # 例 "TENX personal research <your-address@example.com>"
    # 未設定のままEDGARバッチを動かすと ValueError で落ちる(規約違反を
    # 黙って犯さないため)。
    edgar_user_agent: str | None = None
```

`.env.example` にプレースホルダ行を追加する。**実アドレスをコミットしないこと。**

### 30.3.4 追跡対象銘柄の決め方

ユニバースは数千銘柄あり、全件のEDGARを毎日見に行くのは現実的でない(そして無意味——ほとんどが検討対象にすらならない)。**追跡対象は次の和集合**とする。

1. `config/positions.yaml` に載っている保有銘柄(フェーズ6。無ければ空)
2. 直近スコア日のランキング上位 N 件(`max_tracked_tickers` から1と3の分を引いた残り)
3. `research/` にノートが存在する銘柄(検討中の銘柄)

**保有銘柄を必ず含めるのが要点。** ランキング圏外に落ちた保有銘柄こそ 8-K の監視が要る。順位が下がったこと自体は売る理由にならない(元文書 第12節)が、監視をやめる理由にもならない。

実装:`src/autoscreener/batch/collect_filings.py` の `select_tracked_tickers(session, *, limit) -> list[Ticker]`。

### 30.3.5 filings テーブル

```python
class Filing(Base):
    """SEC提出書類のメタデータ(30.3.5)。

    本文は保存しない。数十MBの文書を全銘柄ぶん貯めるとDBが破裂し、
    しかも再取得はいつでもできる(SECのアーカイブは消えない)。保存するのは
    **判定に使った結論と、その根拠を人間が確認しに行くためのURL**だけ。
    """

    __tablename__ = "filings"
    __table_args__ = (
        UniqueConstraint("ticker_id", "accession_number", name="uq_filing_ticker_accession"),
        Index("ix_filings_ticker_filed", "ticker_id", "filed_date"),
        Index("ix_filings_form_filed", "form", "filed_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    cik: Mapped[str] = mapped_column(String(10))
    accession_number: Mapped[str] = mapped_column(String(25))
    form: Mapped[str] = mapped_column(String(20))
    # 14.3:**提出日がポイントインタイムの基準**である。report_date(決算期末)
    # ではない——期末の数字は、提出されるまで市場も我々も知りようがない。
    filed_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    report_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    items: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # 8-Kのアイテム番号
    primary_document: Mapped[str | None] = mapped_column(String(200), nullable=True)
    document_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # 30.4:本文解析の結果(going_concern / material_weakness / 抜粋)。
    # 解析していない場合は NULL。「解析した結果なにも無かった」(空dict)と
    # 「まだ解析していない」(NULL)を区別できるようにする。
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

### 30.3.6 収集バッチ

**新規モジュール**:`src/autoscreener/batch/collect_filings.py`

```python
def collect_filings(
    *, as_of: datetime.date | None = None, symbols: list[str] | None = None
) -> dict[str, int]:
    """追跡対象銘柄の提出書類メタデータを取得し `filings` へ upsert する。

    戻り値は {"tickers": n, "new_filings": n, "skipped_no_cik": n, "failures": n}。
    """
```

- 収集対象フォーム(定数 `TRACKED_FORMS`):`8-K`, `10-K`, `10-Q`, `NT 10-K`, `NT 10-Q`, `S-3`, `S-3ASR`, `424B5`, `424B4`, `DEF 14A`, `4`, `SC 13D`, `SC 13G`, `UPLOAD`, `CORRESP`, `25-NSE`, `15-12B`
- **既存行は上書きしない**(`accession_number` は不変)。新規のみ INSERT。
- 1銘柄の失敗で全体を止めない。`collection_logs` に `ticker_id` つきで記録する(status は `success` / `permanent_failure` / `empty_response` を流用)。
- `daily_pipeline.py` への差し込み位置:**`apply_gates` の後、`run_scoring` の前**ではなく、**`run_scoring` の後**。理由——追跡対象の選定に当日のランキングを使うため、スコアが確定してからでないと対象が決まらない。失敗してもパイプライン全体は止めない(バックアップと同じ扱い)。

### 30.3.7 フェーズ2の受け入れ基準

- [ ] `EDGAR_USER_AGENT` 未設定で `EdgarClient` を生成すると `ValueError` になり、メッセージに「.env に EDGAR_USER_AGENT を設定」と書いてある
- [ ] `RateLimiter(8.0)` で20回 `acquire()` すると経過時間が 2.3秒以上(= 19/8 秒以上)
- [ ] `responses` でスタブした `submissions` JSON から `FilingRecord` が正しく組み立つ(並列配列のインデックス取り違えが無い)
- [ ] `items` が `"Item 2.02: Results..."` 形式でも `["2.02"]` にパースされる
- [ ] HTTP 403 が `PermanentFailure` になり、リトライされない
- [ ] `tenx refresh-cik-map` 実行後、既知の銘柄(例:AAPL → `0000320193`)の `cik` が埋まる
- [ ] `tenx collect-filings --symbols AAPL` が2回連続で実行でき、2回目の `new_filings` が 0
- [ ] 新規テスト:`tests/unit/test_edgar_client.py`、`tests/unit/test_collect_filings.py`、`tests/unit/test_cik_map.py`

---

## 30.4 フェーズ3:レッドフラグ判定(即死要因の自動検出)

> **狙い**:元文書の実務ワークフロー工程3(即死要因のスクリーニング・15分)を自動化する。**候補の相当数がこの段階で落ちる**というのが元文書の主張であり、ここが本計画で最も費用対効果が高い。

### 30.4.1 判定の対象と重み

**新規モジュール**:`src/autoscreener/screening/red_flags.py`(純粋関数。DBに触らない)

```python
"""提出書類から読み取れる「即死要因」の判定(30.4)。

元文書 第01節・第05節。**除外ゲートではない**(30.1.3 原則3)。ゲートに入れる
と擬似バックテストの母集団定義が今日以降の収集状況に汚染される。ここは
表示とアラートのための独立した層であり、`evaluate_gates` からは呼ばれない。

重大度は3段階:
- BLOCKING … 新規建てを止める。人間が理由を確認するまで検討を進めない
- WARNING  … サイズを落とす/条件つきで進む
- INFO     … 事実として知っておく(判断は人間)
"""

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"


@dataclass(frozen=True)
class RedFlag:
    code: str
    severity: str
    detected_on: datetime.date       # 提出日(= 我々が知りえた最初の日)
    source_accession: str | None
    detail: str                      # 日本語の説明文。UIにそのまま出す
    document_url: str | None         # 一次情報へのリンク。必ず出す


def evaluate_red_flags(
    filings: list[FilingView], *, as_of: datetime.date, lookback_days: int = 400
) -> list[RedFlag]:
    """新しい順に並べて返す。`lookback_days` より古い提出は見ない。"""
```

**判定表**(この表がそのまま実装の仕様。定数は `red_flags.py` に持つ):

| コード | 検出条件 | 重大度 | 元文書の根拠 |
|---|---|---|---|
| `restatement` | 8-K item **4.02** | BLOCKING | 第01節「事実上の売り」 |
| `auditor_change` | 8-K item **4.01** | BLOCKING | 第05節・第12節 |
| `listing_deficiency` | 8-K item **3.01** | BLOCKING | 第05節「上場基準抵触」 |
| `late_filing` | フォーム `NT 10-K` / `NT 10-Q` | BLOCKING | 第01節「原則として新規建てを停止」 |
| `going_concern` | 直近 10-K / 10-Q 本文に継続企業の前提の記載(30.4.2) | BLOCKING | 第05節「あれば即座に除外」 |
| `material_weakness` | 直近 10-K 本文に内部統制の重要な不備の記載 | WARNING | 第05節 |
| `officer_departure` | 8-K item **5.02** | WARNING | 第01節「CFOの突然の退任」 |
| `sec_comment_letter` | フォーム `UPLOAD` / `CORRESP` | WARNING | 第08節「SECが会計処理のどこを疑ったか」 |
| `shelf_registration` | フォーム `S-3` / `S-3ASR` | INFO | 第01節(将来の希薄化枠) |
| `secondary_offering` | フォーム `424B5` | WARNING | 第01節・第04節(実弾の発行) |
| `material_agreement` | 8-K item **1.01** | INFO | 第01節 |
| `activist_stake` | フォーム `SC 13D` | INFO | 第01節 |
| `delisting_form` | フォーム `25-NSE` / `15-12B` | BLOCKING | 第05節 |

**期間の扱い**:`late_filing` と `secondary_offering` は**古くなれば意味が薄れる**(1年前のNTは既に解決している可能性が高い)。`lookback_days` の既定を400日としつつ、**コードごとに有効期間を持たせる**:

```python
# 意味が持続する期間(日)。これを過ぎたフラグは返さない。
FLAG_TTL_DAYS: dict[str, int] = {
    "restatement": 730,        # リステートメントの影響は2年残ると見る
    "auditor_change": 365,
    "listing_deficiency": 365,
    "late_filing": 180,        # 半年で「解決済みか未解決か」は決算1回で分かる
    "officer_departure": 365,
    "sec_comment_letter": 365,
    "shelf_registration": 1095,  # シェルフの有効期間は概ね3年
    "secondary_offering": 730,   # 資本配分の癖を見るため長めに残す
    "material_agreement": 365,
    "activist_stake": 730,
    "delisting_form": 3650,
}
```

`going_concern` / `material_weakness` は**最新の10-K/10-Qの解析結果のみ**を見る(TTLではなく「直近の提出で消えたか」で判定する。これが正しい——記載が消えたなら事実として解消している)。

### 30.4.2 本文からの継続企業の前提・内部統制の検出

**やり方**:`EdgarClient.fetch_document_text` でプレーンテキスト化し、正規表現でマッチさせる。**LLMに読ませない**(原則1:再現性が無く、検証もできない判定をブロッキング条件にしてはならない)。

```python
# 継続企業の前提に関する重要な不確実性。監査報告書と流動性の節に定型句で現れる。
# "going concern" 単独では、会計方針の説明("prepared on a going concern basis")
# にも当たってしまうため、**substantial doubt との共起**を要求する。
_GOING_CONCERN_PATTERN = re.compile(
    r"substantial\s+doubt[^.]{0,200}?going\s+concern"
    r"|going\s+concern[^.]{0,200}?substantial\s+doubt",
    re.IGNORECASE | re.DOTALL,
)

# 内部統制の重要な不備。"material weakness" は定型句で、否定形
# ("no material weakness")との区別が要る。
_MATERIAL_WEAKNESS_PATTERN = re.compile(r"material\s+weakness(es)?", re.IGNORECASE)
_MATERIAL_WEAKNESS_NEGATION = re.compile(
    r"(no|not|without)\s+(any\s+)?material\s+weakness(es)?"
    r"|material\s+weakness(es)?\s+(were|was|has|have)\s+(not|no longer)",
    re.IGNORECASE,
)
```

- **マッチした前後200文字を `analysis.excerpt` に保存する。** 人間が「本当にそうか」を10秒で確認できるようにするため。抜粋の無いブロッキング判定は信用されず、結局全部を人間が読み直すことになる。
- `material_weakness` は、肯定パターンがマッチし、かつ**同じ位置の否定パターンがマッチしない**ときのみ真とする。誤検知を減らせないなら **WARNING 止まりにしておく**(BLOCKINGに昇格させない)。この非対称な扱いは意図的:誤ってブロックするコストは、誤って見逃すコストより大きい局面と小さい局面があり、内部統制は後者。
- 解析結果は `filings.analysis` に次の形で入れる:

```json
{"going_concern": true, "material_weakness": false, "excerpt": "...", "analyzed_at": "2026-08-28", "truncated": false}
```

`truncated` は `max_bytes` で切り捨てたかどうか。**切り捨てた文書で「無し」と判定した場合、それは「無い」ではなく「見えていない」**ので、UIは区別して表示する。

### 30.4.3 API

`CandidateDetail` に追加:

```python
    # 30.4:提出書類から読み取れる即死要因・注意事項。新しい順。
    red_flags: list[RedFlagView] = []
    # 追跡対象外でEDGARを一度も見ていない銘柄は None(空リストと区別する。
    # 「調べて何も無かった」と「調べていない」を同じ表示にしてはならない)。
    filings_checked_on: datetime.date | None = None
```

```python
class RedFlagView(BaseModel):
    code: str
    severity: str
    detected_on: datetime.date
    detail: str
    document_url: str | None
```

`CandidateSummary` には**件数だけ**を載せる(一覧の応答を膨らませないため):

```python
    blocking_flag_count: int = 0
    warning_flag_count: int = 0
```

新規エンドポイント `GET /filings/{ticker}`:その銘柄の `filings` を新しい順に返す(既定50件)。**詳細画面の「一次情報へ」の導線**であり、判定の透明性を担保する。

### 30.4.4 フロントエンド(フェーズ3分)

- `frontend/src/warnings.ts` に13コードぶんの `label` / `description` を追加。既存の警告バッジと**同じ表示機構に乗せる**が、`severity` で色分けする。
- `TickerDetailPage.tsx` に「提出書類とレッドフラグ」節を追加。各フラグに `document_url` へのリンクを必ず出す。
- ランキング一覧では、`blocking_flag_count > 0` の銘柄に目立つバッジを出す。**行を隠さない**——隠すと、なぜ消えたのか分からなくなる(既存の除外銘柄画面と同じ思想)。

### 30.4.5 フェーズ3の受け入れ基準

- [ ] 8-K の `items` が `["4.02"]` の `filings` 行1件から `restatement`(BLOCKING)が1件返る
- [ ] `NT 10-Q` が200日前なら `late_filing` が返らない(TTL 180日)
- [ ] `"...raise substantial doubt about the Company's ability to continue as a going concern..."` を含む文書で `going_concern=true`
- [ ] `"...prepared on a going concern basis..."` だけの文書で `going_concern=false`(共起要求の検証)
- [ ] `"no material weaknesses were identified"` で `material_weakness=false`
- [ ] `filings` が0件の銘柄で `red_flags=[]`、`filings_checked_on=None` が返り、UIが「未確認」と表示する
- [ ] `evaluate_gates` のテストが1つも変わっていない(ゲートに手を入れていないことの証明)
- [ ] 新規テスト:`tests/unit/test_red_flags.py`、`tests/unit/test_filing_analysis.py`

---

## 30.5 フェーズ4:XBRL突合(yfinance値の検算)

> **狙い**:元文書 第00節の「ポジションを取ると決めた銘柄は、売上・株式数・現金・負債の4つだけは必ずEDGARの原本で突き合わせる」を自動化する。**モデルの入力そのものが二次加工データである**という既知の弱点(13.5・B-7・E-1)に対する直接の対処。

### 30.5.1 対象タグ

| 概念 | タクソノミ | タグ(優先順) | 単位 |
|---|---|---|---|
| 売上高(年次) | us-gaap | `RevenueFromContractWithCustomerExcludingAssessedTax` → `Revenues` → `SalesRevenueNet` | USD |
| 現金及び現金同等物 | us-gaap | `CashAndCashEquivalentsAtCarryingValue` | USD |
| 負債合計 | us-gaap | `Liabilities` | USD |
| 発行済株式数 | dei | `EntityCommonStockSharesOutstanding` | shares |

**フォールバック順を持つのが要点。** 単一タグ決め打ちだと、収益認識基準の適用時期によってタグが変わる銘柄で軒並み欠損する。**最初に値が取れたタグを使い、どのタグを使ったかを保存する**(後から「なぜこの値になったか」を追えるようにするため)。

### 30.5.2 xbrl_facts テーブル

```python
class XbrlFact(Base):
    """SEC XBRL の実績値(30.5)。

    companyfacts の全量ではなく、**モデルの入力と突き合わせる4概念だけ**を
    保存する。全量を入れると1銘柄あたり数MBのJSONになり、しかも使わない。

    `filed_date` を必ず持つのは 14.3(先読みバイアス)のため。決算期末
    (`period_end`)の数字は、提出されるまで知りようがない。過去日の再現に
    使うときは `filed_date <= 基準日` で絞る。
    """

    __tablename__ = "xbrl_facts"
    __table_args__ = (
        UniqueConstraint(
            "ticker_id", "tag", "period_end", "form", "accession_number",
            name="uq_xbrl_fact",
        ),
        Index("ix_xbrl_facts_ticker_tag_end", "ticker_id", "tag", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    taxonomy: Mapped[str] = mapped_column(String(20))   # "us-gaap" / "dei"
    tag: Mapped[str] = mapped_column(String(100))
    unit: Mapped[str] = mapped_column(String(20))       # "USD" / "shares"
    period_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[datetime.date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Numeric(24, 4))
    form: Mapped[str] = mapped_column(String(20))
    accession_number: Mapped[str] = mapped_column(String(25))
    filed_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    fiscal_year: Mapped[int | None] = mapped_column(nullable=True)
    fiscal_period: Mapped[str | None] = mapped_column(String(4), nullable=True)  # FY/Q1..Q4
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

**同じ期に複数の値がある場合**(修正再提出・後続の10-Kでの再掲)は、**すべて行として保存し、読み出し側が `filed_date` の最も新しいものを採る**。上書きしないのは、リステートメントの検出そのものが情報だから(値が変わった事実が `restatement` フラグの裏付けになる)。

### 30.5.3 突合ロジック

**新規モジュール**:`src/autoscreener/validation/reconciliation.py`(純粋関数)

```python
"""yfinance値とSEC XBRL値の突合(30.5.3)。

元文書 第00節。**桁違いの誤りを潰すのが目的**であり、小数点以下の一致を
求めるものではない。yfinanceの値はTTM・調整後・通貨換算後であることがあり、
数%の差は正常。閾値を緩めに取り、**それでも合わないものだけを拾う**。
"""

# 相対差がこれを超えたら不一致とみなす。25%は「四半期1つ分の差」に相当し、
# TTMの期ずれで説明できる範囲の外側。
DEFAULT_TOLERANCE = 0.25
# 桁違い(10倍以上)は別扱い。単位の罠(13.5の debtToEquity 型の欠陥)の疑い。
MAGNITUDE_THRESHOLD = 5.0


@dataclass(frozen=True)
class ReconciliationItem:
    concept: str            # "revenue" / "shares_outstanding" / "cash" / "liabilities"
    model_value: float | None   # モデルが使っている値(yfinance由来)
    sec_value: float | None     # XBRL値
    sec_tag: str | None
    sec_period_end: datetime.date | None
    sec_filed_date: datetime.date | None
    relative_diff: float | None
    status: str             # "match" / "mismatch" / "magnitude_mismatch" / "unavailable"


def reconcile(
    model_inputs: dict, facts: list[XbrlFactView], *, as_of: datetime.date,
    tolerance: float = DEFAULT_TOLERANCE,
) -> list[ReconciliationItem]:
    """4概念それぞれについて突合する。片方が無ければ status="unavailable"。"""
```

- `model_inputs` は `scores.inputs`(27.24でモデルの入力そのものが保存済み)から取る。**raw_snapshots を読み直さない**——`inputs` を保存した理由がまさにこれ。
- **`unavailable` を `mismatch` と混ぜない。** 突き合わせられなかったことは、食い違ったことではない。
- **突合は追跡対象銘柄だけで行う。** ユニバース全件のcompanyfactsを毎日取るのは非現実的。

### 30.5.4 警告バッジへの接続

`routes.py` の `_compute_warnings` は `factors` からしか判定していない。ここに**外部由来の警告を合流させる**必要があるので、シグネチャを変える:

```python
def _compute_warnings(
    factors: dict | None,
    survival_probability: float | None,
    external: list[str] | None = None,   # 30.5.4:突合・レッドフラグ由来の警告コード
) -> list[str]:
```

新コード:`sec_value_mismatch`(WARNING)、`sec_magnitude_mismatch`(BLOCKING相当・強調表示)。`frontend/src/warnings.ts` に説明を追加する。文言案:

> **SEC原本と数値が不一致**:モデルが使っている売上/株式数/現金/負債のいずれかが、SECに提出された原本(XBRL)と大きく食い違っています。モデルの入力は yfinance の二次加工データであり、単位の取り違えや分割未調整が実際に見つかっています。詳細画面で該当項目と原本へのリンクを確認してください。

### 30.5.5 バッチとCLI

- `src/autoscreener/batch/collect_xbrl_facts.py`:`collect_xbrl_facts(symbols=None) -> dict[str,int]`。追跡対象銘柄の companyfacts を取り、4概念ぶんを upsert。
- CLI:`tenx collect-xbrl [--symbols ...]`、`tenx reconcile <TICKER>`(結果を表形式で標準出力に出す。手作業の検算に使えるように)。
- 日次パイプラインでは **週1回(月曜)** 実行する。財務データは四半期に1回しか変わらないので日次は無駄。

### 30.5.6 フェーズ4の受け入れ基準

- [ ] companyfacts のスタブJSONから4概念が抽出され、フォールバックタグの優先順が効く
- [ ] 同一期に2件(原提出と再提出)ある場合、`filed_date` の新しいほうが突合に使われる
- [ ] `model_value` が SEC 値の1000倍のとき `magnitude_mismatch` になる
- [ ] 片方が `None` のとき `unavailable` であり、`mismatch` にならない
- [ ] 突合結果が `CandidateDetail` に出て、`warnings` に `sec_value_mismatch` が合流する
- [ ] `tenx reconcile AAPL` が人間可読の表を出す
- [ ] 新規テスト:`tests/unit/test_reconciliation.py`、`tests/unit/test_xbrl_facts.py`

---

## 30.6 フェーズ5:将来の希薄化

> **狙い**:元文書 第00節の表が指摘する**モデルの構造的な穴**——「発行済株式数の外挿は過去実績ベースであり、未使用のシェルフ枠・ATM残枠・転換社債・未行使SOという**予約済みの希薄化**を一切見ていない」——を、少なくとも**可視化する**。

### 30.6.1 何ができて、何ができないか(先に確定させる)

| 項目 | 自動取得 | 理由 |
|---|---|---|
| S-3 / S-3ASR の提出履歴と登録上限額 | **半自動** | 提出の有無と日付はメタデータから取れる。金額は表紙の記載で、抽出は不安定 |
| 424B5 の提出履歴(実弾の発行) | **可能** | メタデータのみで足りる。回数と時期が資本配分の癖を示す |
| ATM残枠 | **不可** | 10-Qの注記本文。文言が会社ごとに違う |
| 未行使SO・未確定RSU | **不可** | DEF 14A / 注記。表形式で会社ごとに違う |
| 転換社債の転換価格が固定か変動か | **不可** | 注記本文の読解が要る |

**したがってこのフェーズの成果物は「自動で埋まる欄」と「人間が埋める欄」の混在になる。** これを曖昧にすると、空欄が「無い」と誤読される。**UIは必ず「未入力」と「該当なし」を別の表示にすること。**

### 30.6.2 自動で出すもの

`CandidateDetail` に追加:

```python
class DilutionOutlook(BaseModel):
    """30.6:将来の希薄化(モデルの株数外挿に入っていない予約済み分)。"""

    # 自動:提出履歴から
    shelf_filings: list[FilingRef]      # S-3 / S-3ASR(直近3年)
    offering_filings: list[FilingRef]   # 424B5(直近3年)
    offerings_last_3y: int
    # 自動:モデルが使っている過去実績ベースの希薄化率(比較対象として並べる)
    historical_dilution_rate: float | None
    # 人間が research/<TICKER>.md に書いた値(30.7)。未入力なら None
    remaining_shelf_capacity_usd: float | None
    atm_remaining_usd: float | None
    unexercised_options_ratio: float | None      # 発行済株式数に対する比率
    has_variable_conversion_price: bool | None   # True なら元文書は「原則候補から外す」
    # 自動:上の人間入力と時価総額から算出。入力が無ければ None
    reserved_dilution_ratio: float | None        # (シェルフ残枠 + ATM残枠) ÷ 時価総額
```

**`reserved_dilution_ratio >= 0.20` で警告バッジ `heavy_reserved_dilution` を出す**(元文書 第01節「20%を超えるなら7年で10倍という想定に対して重い負担」)。

### 30.6.3 人間が埋める欄の置き場所

フェーズ6のノート(`research/<TICKER>.md`)のフロントマターに `dilution:` ブロックとして持つ(30.7.2 のスキーマ参照)。**別ファイルを作らない**——1銘柄の情報が2ファイルに分かれると、片方だけ更新される。

### 30.6.4 フェーズ5の受け入れ基準

- [ ] 直近3年の S-3 / 424B5 が詳細画面に日付順で並び、それぞれ原本にリンクする
- [ ] ノートに `dilution.remaining_shelf_capacity_usd` を書くと `reserved_dilution_ratio` が計算される
- [ ] ノートが無い/該当項目が無い銘柄で、UIが「未入力」と表示する(0や「なし」と表示しない)
- [ ] `reserved_dilution_ratio = 0.25` で `heavy_reserved_dilution` バッジが出る
- [ ] 新規テスト:`tests/unit/test_dilution_outlook.py`

---

## 30.7 フェーズ6:保有・投資ノート・四半期モニタリング

> **狙い**:元文書 第12節の運用自動化の指摘——「表の上4行は既存データから機械的に計算できる。既存のパイプラインに**保有銘柄フラグと閾値アラート**を足すのが、投資プロセス全体で最も費用対効果の高い追加開発になる」——をそのまま実装する。

### 30.7.1 保有銘柄(config/positions.yaml)

```yaml
# 30.7.1:保有銘柄。**アプリはこのファイルを読むだけで、書かない**(30.1.1 原則2)。
# 追加・売却は手で編集し、gitにコミットする。コミット履歴がそのまま売買記録になる。
positions:
  - ticker: ABCD
    opened_on: 2026-08-20
    shares: 120
    cost_basis_usd: 14.32     # 1株あたり取得単価(USD)
    note: research/ABCD.md    # 省略時は research/<TICKER>.md を見る
    binary_event: false       # 二値イベントを抱えるか(サイズ上限が半分になる)
    closed_on: null           # 売却済みなら日付を入れる。行は消さない(記録のため)
```

**売却しても行を消さないのが要点。** 消すと事後レビュー(元文書 第13節)の材料が消える。`closed_on` が入っている行はモニタリング対象から外れるが、記録としては残る。

`src/autoscreener/config.py` に `PositionsConfig` / `Position` を追加し、`load_positions_config()` を用意する。ファイルが無ければ**空のリストを返す**(エラーにしない。保有が無い状態は正常)。

### 30.7.2 投資ノート(research/<TICKER>.md)

元文書 第13節の表がそのままスキーマになる。**YAMLフロントマター + 自由記述の本文**。

```markdown
---
ticker: ABCD
created_on: 2026-08-28
thesis: |
  3文以内。なぜこの会社が7年で10倍になり得るのか。
assumptions:
  # TENXの値と自分の値の両方を書く。差の出どころを後から集計できるようにするため
  revenue_growth: { model: 0.42, mine: 0.30 }
  terminal_margin: { model: 0.55, mine: 0.50 }
  terminal_multiple: { model: 12.0, mine: 9.0 }
  dilution_rate: { model: 0.06, mine: 0.10 }
model_divergence: |
  TENXより弱気。理由:成長率が上限クランプに当たっており実力が測れていない。
premortem:
  # 元文書 第08節:失敗要因3つと、それぞれの「事前に観測可能な先行指標」
  - cause: 主要顧客の契約が更新されない
    indicator: customer_concentration_disclosed_drop
    detail: 10-Kの10%超顧客の記載が消える、または売上が2四半期連続で減速
  - cause: 価格競争の開始
    indicator: gross_margin_decline
    detail: 粗利率が2四半期連続で低下
  - cause: 資金繰りに追われた増資
    indicator: share_count_growth
    detail: 株式数が年率15%を超えて増加
sizing:
  amount_usd: 4000
  rationale: ADV制約(上限$3,200)ではなく規律側が効いた。二値イベント無し
verification_date: 2026-11-05   # 次にテーゼが試される日(次回決算)
dilution:
  remaining_shelf_capacity_usd: 150000000
  atm_remaining_usd: 40000000
  unexercised_options_ratio: 0.11
  has_variable_conversion_price: false
review: null   # 事後レビュー。結果が出てから追記する
---

ここから下は自由記述。事業の理解、モートの型、TAMのボトムアップ計算、
決算説明会Q&Aで気になった点、ショートレポートの検証結果など。
```

**新規モジュール**:`src/autoscreener/research/notes.py`

```python
"""投資ノートの読み込みと検証(30.7.2)。

元文書 第13節。**アプリは書かない、読むだけ**(30.1.1 原則2)。
「建てる前に書くこと」「後から書き換えないこと」という要件は、gitの
コミット履歴が担保する——DBのUPDATEでは担保できない。

**記入漏れの検出がこのモジュールの主目的**である。元文書の実務ワークフロー
工程9は「埋められない項目があるうちは建てない」と定めており、それを人間の
自制心ではなくアプリの表示で支える。
"""

REQUIRED_FIELDS = ("thesis", "assumptions", "premortem", "sizing", "verification_date")
MIN_PREMORTEM_ITEMS = 3   # 元文書 第08節「失敗要因を3つ書き出す」


@dataclass(frozen=True)
class ResearchNote:
    ticker: str
    path: Path
    front_matter: dict
    body: str
    missing_fields: list[str]     # 記入漏れ。空なら「建ててよい」状態
    is_complete: bool


def load_note(ticker: str, directory: Path | None = None) -> ResearchNote | None:
    """`research/<TICKER>.md` を読む。無ければ None。"""


def load_all_notes(directory: Path | None = None) -> dict[str, ResearchNote]:
    """検討中の銘柄一覧(30.3.4 の追跡対象選定に使う)。"""
```

- **フロントマターの検証は Pydantic で行うが、`missing_fields` を例外にしない。** 書きかけのノートは正常な状態であり、そこでプロセスを落とすのは間違い。壊れたYAML(パースできない)だけをエラーにする。
- `research/` ディレクトリと `research/TEMPLATE.md`(上記の雛形)をリポジトリに追加する。

### 30.7.3 四半期モニタリング指標

**新規モジュール**:`src/autoscreener/screening/monitoring_metrics.py`(純粋関数)

元文書 第12節の表の上4行。**すべて既存データから計算できる**(新規取得ゼロ)。

| 指標 | 出所(既存) | 危険信号の既定閾値 |
|---|---|---|
| 売上成長率(YoY) | `raw_snapshots.payload.quarterly_income_stmt` | 2四半期連続の減速 |
| 粗利率 | 同上(`Gross Profit` / `Total Revenue`) | 2四半期連続の低下 |
| 発行済株式数 | `price_snapshots.shares_outstanding`(13.4で分割調整済み) | 年率換算15%超の増加 |
| キャッシュランウェイ | `exclusion_gates.compute_cash_runway_quarters` を**再利用** | 12か月未満 |

**既存関数を必ず再利用すること。** `exclusion_gates.parse_period_series` / `compute_cash_runway_quarters` / `normalize_financial_currency_value`(13.5の通貨混在対策)がすでにある。同じ計算を2箇所に書くと必ず片方だけ直される。

```python
@dataclass(frozen=True)
class MonitoringMetric:
    code: str
    label: str
    current_value: float | None
    previous_value: float | None
    triggered: bool
    detail: str


def evaluate_monitoring(
    quarterly_income_stmt: dict,
    quarterly_cash_flow: dict,
    total_cash: float | None,
    share_counts: list[tuple[datetime.date, int]],
    info: dict,
    thresholds: MonitoringThresholds,
) -> list[MonitoringMetric]:
    ...
```

**新規設定**:`config/monitoring.yaml`

```yaml
# 30.7.3:保有銘柄の四半期モニタリング閾値(元文書 第12節)。
# **閾値は売却条件ではない**。点灯したら「価格に関係なく判断をやり直す」
# ための合図であり、機械的な売りシグナルとして使ってはならない(第11節 売却規律)。
revenue_growth_deceleration_quarters: 2
gross_margin_decline_quarters: 2
share_count_annual_growth_ceiling: 0.15
cash_runway_floor_months: 12
```

### 30.7.4 alerts テーブルと monitor バッチ

```python
class Alert(Base):
    """保有・追跡銘柄で新たに点灯した監視項目(30.7.4)。

    レッドフラグ(30.4)や監視指標(30.7.3)は毎日**再評価すれば同じ結論**が
    出る。それでも行として保存するのは、**「いつ初めて点灯したか」が
    それ自体で情報**だから——決算の翌日に点いたのか、3か月前から点いていたの
    かで、対応は変わる。導出結果ではなく状態遷移を記録するテーブルである。
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("ticker_id", "code", "triggered_on", name="uq_alert_ticker_code_date"),
        Index("ix_alerts_triggered", "triggered_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(ForeignKey("tickers.id"), index=True)
    code: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(20))   # "red_flag" / "metric" / "premortem"
    triggered_on: Mapped[datetime.date] = mapped_column(Date)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 人間が見たことを記録する。CLI `tenx ack <id>` から書く(APIは読み取り専用)
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

**新規バッチ**:`src/autoscreener/batch/run_monitoring.py`

```python
def run_monitoring(as_of: datetime.date | None = None) -> dict[str, int]:
    """保有・追跡銘柄を評価し、**新規に点灯したものだけ** alerts に書く。

    戻り値は {"tickers": n, "new_alerts": n, "already_open": n}。
    """
```

- **既に同じ `(ticker_id, code)` で未解消のアラートがあれば新規行を作らない。** 毎日同じアラートが積み上がると、通知が無意味になる(アラート疲れ。18.7・E-2で一度学んだ教訓)。
- **プレモーテム指標との接続**:ノートの `premortem[].indicator` が `monitoring_metrics` のコードと一致する場合、その指標が点灯したら `source="premortem"` として**別のアラートを立てる**。これが元文書 第08節と第11節をつなぐ要——「自分が事前に決めた反証条件が点灯した」は、汎用の閾値超過より重い。
- 日次パイプラインの**最後**(バックアップの前)に差し込む。失敗しても止めない。
- 新規アラート件数を `monitoring.py` の既存の仕組みでログに出す(BLOCKING が出たら `logger.error`)。

### 30.7.5 API

新規エンドポイント3つ:

| エンドポイント | 返すもの |
|---|---|
| `GET /positions` | 保有一覧。各行に最新スコア・監視指標・未解消アラート・ノートの記入状況・現在のポジション比率 |
| `GET /alerts?days=30&severity=blocking` | 直近アラート(新しい順) |
| `GET /research/{ticker}` | ノートのフロントマターと記入漏れ項目(本文はMarkdownのまま返す) |

`GET /positions` のレスポンスには**ポートフォリオ集計**を必ず含める(元文書 第11節「相関と集中の管理」):

```python
class PortfolioSummary(BaseModel):
    total_cost_usd: float
    position_count: int
    sector_weights: dict[str, float]
    sector_cap_breaches: list[str]          # config の sector_cap を超えたセクター
    position_cap_breaches: list[str]        # per_position_cap を超えた銘柄
    unprofitable_share: float | None        # 赤字銘柄の合計比率(金利感応度の集中度)
```

### 30.7.6 フェーズ6の受け入れ基準

- [ ] `config/positions.yaml` が存在しない状態で `GET /positions` が空リストと 200 を返す
- [ ] `research/ABCD.md` の `thesis` を消すと `missing_fields` に `thesis` が入り、`is_complete=false` になる
- [ ] `premortem` が2件しかないと `missing_fields` に入る(3件必要)
- [ ] 壊れたYAMLフロントマターでは例外になり、メッセージにファイルパスと行番号が出る
- [ ] 粗利率が2四半期連続で低下している銘柄で `gross_margin_decline` が点灯する
- [ ] 同じアラートが2日連続で発生しても `alerts` の行は1件のまま
- [ ] プレモーテム指標に紐づいたアラートが `source="premortem"` で立つ
- [ ] `closed_on` が入った保有がモニタリング対象から外れる(が、行は残る)
- [ ] セクター比率が `sector_cap` を超えると `sector_cap_breaches` に入る
- [ ] 新規テスト:`tests/unit/test_research_notes.py`、`tests/unit/test_monitoring_metrics.py`、`tests/unit/test_run_monitoring.py`、`tests/unit/test_positions_config.py`

---

## 30.8 フェーズ7:マクロ(FRED)

> **狙い**:元文書 第09節。モデルのマルチプル項は**今の金利環境が7年続く前提**を暗黙に置いている。3系列を月次で記録し、レジームの変化を可視化する。

### 30.8.1 対象系列

| series_id | 内容 | なぜ |
|---|---|---|
| `DGS10` | 米10年債利回り | 割引率の水準 |
| `DFII10` | 米10年実質金利 | 名目と分けて見ないとインフレ由来の変化を取り違える |
| `BAMLH0A0HYM2` | ハイイールドOAS | **赤字企業の資金調達環境**。拡大は希薄化条件の悪化を先行して示す |

**取得方法**:FRED APIはAPIキーが要る。キー不要の CSV エンドポイント(`https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}`)でも取れるが、**APIキー方式を採る**(`.env` の `FRED_API_KEY`)。理由——CSVエンドポイントは公式のAPIではなくグラフ描画用であり、規約上の位置づけが曖昧。キーは無料で即発行できる。

**キーが無い場合はこの機能を無効にする**(`fred.enabled: false` と同じ扱いで、UIは「未設定」と表示)。フェーズ7が無くても他は全部動く。

### 30.8.2 実装

- テーブル `macro_series`(`series_id` / `observation_date` / `value` / `created_at`、UNIQUE(series_id, observation_date))
- `src/autoscreener/collectors/fred_client.py`(EDGARクライアントと同じ構造。レート制御は緩くてよい)
- `src/autoscreener/batch/collect_macro.py` — 週次(月曜)実行
- `GET /macro` — 各系列の最新値、3か月前・1年前との差、および**直近1年の系列**(スパークライン用)
- CLI:`tenx collect-macro`

### 30.8.3 表示の方針(重要)

**マクロ値からスコアを自動調整してはならない。** 元文書は「金利上昇局面ではTENXの終端マルチプル前提を保守側に置き換えて再計算する(設定は `config/scoring.yaml` で変更できる)」と書いており、これは**人間が設定を変える**という意味である。マクロ値をスコアに自動で織り込むと、モデルの検証(27.8のバックテスト)が二重に効いた交絡を含むことになる。**表示と、人間への提案(「HY OASが1年で+200bp拡大しています。滑走路12か月未満の保有銘柄が3件あります」)に留める。**

### 30.8.4 フェーズ7の受け入れ基準

- [ ] `FRED_API_KEY` 未設定で `GET /macro` が 200 と「未設定」状態を返す(500にしない)
- [ ] 3系列が取得され、`macro_series` に重複行が入らない
- [ ] `GET /macro` が最新値と1年前との差を返す
- [ ] `config/scoring.yaml` がマクロによって自動変更されていない(コードにその経路が無いこと)
- [ ] 新規テスト:`tests/unit/test_fred_client.py`、`tests/unit/test_collect_macro.py`

---

## 30.9 フェーズ8:フロントエンド統合

### 30.9.1 新規ページ

| ルート | ページ | 内容 |
|---|---|---|
| `/positions` | `PositionsPage.tsx` | 保有一覧、ポートフォリオ集計(セクター比率・上限抵触)、未解消アラート |
| `/alerts` | `AlertsPage.tsx` | 直近アラート。重大度で絞り込み。各行から銘柄詳細へ |
| `/macro` | `MacroPage.tsx` | 3系列の現在値と1年推移(`recharts` は既に依存にある) |

`App.tsx` のルーティングと `Layout.tsx` のナビゲーションに追加する。

### 30.9.2 デューデリ・チェックリスト(候補詳細ページ内)

元文書の実務ワークフロー(第15節)の11工程を、**詳細画面のチェックリストとして表示する**。各工程は3状態:

- **自動で判定済み**(工程1〜5の一部):結果とその根拠へのリンクを出す
- **人間が記録済み**(ノートに該当項目がある):記入内容の要約を出す
- **未着手**:何をすればよいかの1行と、外部サイトへの検索リンク

```
工程 01  取扱可否            ✓ SBI証券で取扱あり
工程 02  流動性              ✓ ADV $1.2M / 投入上限 $3,200(板が制約)
工程 03  即死要因            ⚠ NT 10-Q(2026-07-14)— 提出遅延。原則として新規建てを停止
工程 04  数字の原本照合      ✓ 売上・株式数・現金・負債すべてSEC原本と一致
工程 05  希薄化の将来分      ○ 未入力 — シェルフ残枠をノートに記入してください
工程 06  事業の理解          ○ 未入力 — 10-K Item 1 / 直近8回のQ&A [EDGARで開く]
工程 07  経営陣の検証        ○ 未入力 — DEF 14A [EDGARで開く]
工程 08  反証                ○ 未入力 — プレモーテム3件が必要 [ショートレポート検索]
工程 09  サイジングと記録    ○ ノート未完成(不足: thesis, premortem, sizing)
```

**工程6〜8に外部検索リンクを出すのが、この画面の実務的な価値**である。アプリが調査を代行しない代わりに、**次の一手への到達時間をゼロにする**。リンク先:

- EDGAR企業ページ:`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K`
- 会社IR(`raw_snapshots.payload.info.website` があればそれ)
- ショートレポート検索:検索エンジンへの `{ticker} short report` クエリ
- 証券集団訴訟:`https://securities.stanford.edu`(会社名検索)

### 30.9.3 免責表示

元文書 第16節に対応する。フッターまたは初回訪問時に1回:

> このアプリは一次スクリーニングツールであり、投資助言ではありません。ランキング上位という事実だけではポジションを取る根拠になりません(モデルの既知の限界は「モデル検証」画面を参照)。データは yfinance 由来の二次加工であり、私的利用の範囲で使用してください。

既存の `ValidationPage`(モデル検証)へのリンクを必ず添える。**免責文だけを置いて限界の中身に導線が無いのは、免責として機能していない。**

### 30.9.4 フェーズ8の受け入れ基準

- [ ] 3ページが表示され、ナビゲーションから到達できる
- [ ] チェックリストの11工程がすべて表示され、3状態が視覚的に区別できる
- [ ] 未入力工程の外部リンクが正しいURLを開く(CIKが埋まっている銘柄で確認)
- [ ] `npm run build` と `npm run lint` が通る
- [ ] 用語集(`glossary.ts`)に新規語がすべて登録されている

---

## 30.10 実装後に残る課題(この計画の対象外)

1. **残課題R-1(バックテストの生存バイアス)は解消しない。** 本計画はどのフェーズもこれに触れない。ただし `tickers.cik` が入ることで、**EDGAR の Form 25-NSE / 15-12B を過去に遡って収集し、上場廃止イベントの日付を得る**道が開く。これは母集団そのものを復元するものではない(そもそもマスタに無い会社は見つけられない)が、既知の銘柄の廃止日を正確にする効果はある。別計画として検討する。
2. **ATM残枠・転換社債の条項は自動化できていない。** 注記本文の構造化が必要で、現行の正規表現方式では届かない。人間の入力欄として残す(30.6.1)。
3. **決算日カレンダー**は現在 `earnings_dates` の収集を止めている(27.16)。フェーズ6の `verification_date` はノートに手で書く運用。決算日だけを再収集するかは、運用してから決める。
4. **アラートの通知手段はログのみ**(18.7の既定方針を踏襲)。メール・デスクトップ通知は運用してから必要性を判断する。

---

## 30.11 作業の進め方(チェックリスト)

各フェーズについて、この順に進めること。

1. この文書の該当節と、30.0.1 の「読むもの」を読む
2. 設定モデル(`config.py`)とYAMLファイルを先に追加し、`uv run pytest` が通ることを確認する
3. 純粋関数モジュールとそのテストを書く(**DBもネットワークも触らない**)
4. マイグレーションを作る(`uv run alembic revision --autogenerate` → 目視で修正 → `uv run alembic upgrade head`)
5. バッチ/収集層を書く(ネットワークは `responses` でスタブしてテスト)
6. API層(スキーマ → ルート)を書き、`tests/unit/test_api_routes.py` に追加する
7. フロントエンド(型 → クライアント → ページ → 用語集/警告説明)
8. `README.md` を更新する(新CLIコマンド・新設定ファイル・初回セットアップ手順)
9. 受け入れ基準をすべて確認する
10. `uv run pytest` と `cd frontend && npm run build && npm run lint` の両方が通ることを確認する

**フェーズをまたいで並行着手しない。** 各フェーズがDBマイグレーションを持つため、順序が崩れると `down_revision` が競合する。
