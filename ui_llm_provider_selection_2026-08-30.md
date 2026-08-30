# UIからのLLMモデル/プロバイダ選択・生成実行 — 引き継ぎ資料

作成日: 2026-08-30
対象: このタスクを引き継ぐAIモデル / 開発者
状態: **実装済み（2026-08-30）**。853テスト緑・フロントビルド緑。下記「実装結果」を参照。

---

## 実装結果（2026-08-30）

未確定事項1・2は人間が確認済み → **`openai_compat` 1本 / `confirm`必須+30秒レート制限+ロック**。

| プラン項目 | 実装 |
|---|---|
| プロバイダ抽象化 | `llm/client.py` に `LlmProvider` Protocol・`OpenAICompatClient`・`build_provider()`。`LlmClient` は `provider_name="anthropic"` / `supports_batch=True` を持つ Anthropic 実装として不変。 |
| 設定 | `LlmConfig` に `provider` / `base_url` / `send_effort`。`_known_provider` バリデータ。`Settings.openai_api_key`。`config/collection.yaml`・`.env.example` 追記。 |
| 失敗分類 | `llm/errors.py::classify_exception` が `openai.*` 例外も写す（`_openai_classified`、guarded import）。 |
| 指紋 | `prompt_fingerprint(system, model, effort, provider="anthropic")`。**provider が `anthropic` のときは payload に含めない** → 既存の Claude 行の指紋は不変。 |
| バッチ3経路 | `generate_report` / `summarize_filings` / `score_qualitative` は `build_provider()` 経由・fingerprint に `cfg.provider` を渡す。`score_qualitative` は `supports_batch=False` のとき **逐次フォールバック**（`_store_sequential`、`parse_structured` を1銘柄ずつ・半額なし・`--batch-id` 不可）。 |
| 書き込みAPI | `POST /api/v1/llm/report/generate`（`GenerateReportRequest`/`GenerateReportResult`）。`confirm` 無し→400、`build_provider` で `LlmDisabled`→409、進行中→409、30秒以内の再要求→429（Retry-After付き）、`generate_report` 内部で数えた `failures`→502。**ルートで先に `build_provider()` するのが要点**——`generate_report` は `LlmDisabled` を内部で握って0件終了するため。 |
| プロバイダ一覧API | `GET /api/v1/llm/providers`（`configured` = APIキー有無）。宣言順は `/llm/providers`・`/llm/report`・`/llm/report/generate` すべて `/llm/{ticker}` より前。 |
| CLI | `generate-report` に `--provider` / `--model` / `--effort`（YAML編集なしの1回上書き）。 |
| フロント | `api/client.ts::apiPost` + `fetchLlmProviders` / `generateLlmReport`。`api/types.ts` に4型。`LlmReportPage.tsx` に生成パネル（プロバイダ/モデル(datalist自由入力)/effort + confirm ダイアログ + ローディング + 既存ヒット表示）。 |
| テスト | `test_llm_client.py`（OpenAICompat 11件）/ `test_llm_batches.py`（逐次フォールバック2件）/ `test_api_llm.py`（providers + generate 6件）。`test_llm_advisory_isolation.py` は不変で緑。 |

依存追加: `openai>=1.40,<3.0`（`uv.lock` 更新済み）。

### 追加増分（2026-08-30 その2）— base_url / APIキー / モデルをUIから設定

ユーザー指示で、接続設定そのものを UI から編集可能に。人間の確認済み: **保存先は DB テーブル `app_settings`（key-value・alembic あり）** / **APIキーは平文保存・UI には set/未set のみ表示（本体は返さない）**。

| 項目 | 実装 |
|---|---|
| DB | `AppSetting`（`app_settings`: `key` PK / `value` / `updated_at`）。マイグレーション `e4f9a3d2c7b1`（head）。キー名前空間: `llm.provider` / `llm.base_url` / `llm.model` / `llm.effort` / `llm.send_effort` / `secret.anthropic_api_key` / `secret.openai_api_key` |
| 解決層 | `src/autoscreener/runtime_settings.py`。`resolve_llm_config(base?, raw?)` = `load_llm_config()` に `app_settings` の `llm.*` を重ねる（上書き0件なら同一インスタンスを返す）。`resolve_api_key(provider, raw?)` = DB `secret.*` → `.env` の順。`secret_is_set()`。**DB無し・テーブル無し・行無しはすべて「上書き無し」で静かに続行**（`read_all()` が全例外を握る）→ CLI/単体テストは `app_settings` 不要 |
| 配線 | `LlmClient.from_config` / `OpenAICompatClient.from_config` が `resolve_api_key` 経由。3バッチentrypoint は `cfg = config if config is not None else resolve_llm_config()`。`POST /llm/report/generate` は `resolve_llm_config()` 起点。`GET /llm/providers` は `secret_is_set` で `configured` を判定 |
| API | `GET /api/v1/llm/settings`（実効値 + `*_api_key_set` bool + `overridden` リスト。**キー本体は返さない**）。`PUT /api/v1/llm/settings`（`None`=触らない / 文字列 `""`=上書き削除で既定へ / キー `""`=保存済みキー削除）。provider/effort 不正は 422。書き込みは**読み取り専用APIセッションではなくバッチ層の `db.session.session_scope`**（`_write_session_scope`）を通す。`confirm` は不要（課金なし）だが PUT+JSON で素の CSRF を弾く |
| CLI | 変更なし（`--provider/--model/--effort` は既存。`app_settings` は `resolve_llm_config` 経由で自動的に効く） |
| フロント | `api/client.ts::apiSend`(POST/PUT 共通)・`fetchLlmSettings`・`updateLlmSettings`。`api/types.ts` に `LlmSettings`/`LlmSettingsUpdate`。新コンポーネント `components/LlmConnectionSettings.tsx`（provider/base_url/model/effort/send_effort + キー2本、placeholder が set/未set を示す、「キーを削除」「接続設定を既定に戻す」）。`LlmReportPage.tsx` に `<details>「LLM接続設定」`、保存後に `reloadProviders()` |
| テスト | `test_runtime_settings.py`(7件・DB非依存)、`test_api_llm.py` に settings 5件（キー本体を返さない／永続／providers 反映／`""`で解除／422）。`_clean_app_settings` フィクスチャが `app_settings` を丸ごと退避・復元 |

### 追加増分（2026-08-30 その3）— 名前付き接続プロファイル（一覧・編集）

ユーザー指示で、単一スロット（`app_settings`）から **名前付きプロファイルを何件でも保存し1件をアクティブにする** 方式へ差し替え。

| 項目 | 実装 |
|---|---|
| DB | `LlmConnection`（`llm_connections`: `id` / `name` UNIQUE / `provider` / `base_url` / `model?` / `effort?` / `send_effort` / `api_key`（平文）/ `is_active` / timestamps）。`is_active` は `WHERE is_active` 付き部分ユニークで**最大1件**。マイグレーション `f7a1c3e9b5d2`（head）が `llm_connections` を作り **`app_settings` を drop**（同日追加で LLM 専用だったため。downgrade で復元）|
| 解決層 | `runtime_settings.py` を書き直し。`get_active_connection() -> ActiveConnection\|None`。`resolve_llm_config(base?, active=_UNSET)` = アクティブ行の非空 provider/base_url/model/effort/send_effort を yaml に重ねる（`model`/`effort` が空なら yaml の既定へフォールバック）。`resolve_api_key(provider, active=_UNSET)` = アクティブ行の provider が一致すればその `api_key`、さもなくば `.env`。`_UNSET` 番兵で「未指定（DB取得）」と「アクティブ無し（None）」を区別。DB無し/テーブル無しは静かに None |
| API | `GET /llm/connections`（一覧・キー本体なし）、`POST /llm/connections`（作成、`activate` フラグ可、name 重複 409、provider/effort 不正 422）、`PUT /llm/connections/{id}`（部分更新、`base_url`/`model`/`effort` に `""` でクリア、`api_key` `""` で削除）、`DELETE /llm/connections/{id}`（204）、`POST /llm/connections/{id}/activate`（排他）、`POST /llm/connections/deactivate`。`GET /llm/settings` は実効値 + `active_connection_id`/`_name`（`PUT /llm/settings` は廃止）。書き込みは `_write_session_scope`（バッチ層エンジン）|
| フロント | `LlmConnectionSettings.tsx` を削除、`LlmConnectionsManager.tsx` を新設（一覧テーブル + 有効化/編集/削除、新規/編集フォーム、「アクティブを解除」、現在の実効設定サマリ）。`api/client.ts` に `apiSend` を DELETE/204 対応に拡張 + connection 系6関数。`api/types.ts` の `LlmSettings` から `overridden` を削り `active_connection_*` を追加、`LlmConnection*` 型を追加。`LlmReportPage.tsx` の `<details>` を差し替え |
| テスト | `test_runtime_settings.py` を `ActiveConnection` ベースに書き直し（7件）。`test_api_llm.py` の settings 5件 → connections 7件（作成/一覧でキー非露出・name一意・排他activate・`""`クリア・provider不正・削除・generate がアクティブ行を使う）|

CLI は変更なし（`resolve_llm_config()` 経由で自動的にアクティブ行が効く）。

**セキュリティ注記:** API は無認証・CORS は localhost のみ。書き込み系（`POST /llm/report/generate`・`POST|PUT|DELETE /llm/connections*`）は `Content-Type: application/json` 必須で単純フォーム CSRF は preflight で弾かれるが、無認証で平文キーを書ける口ではある（11.1解釈A のローカル個人利用前提で許容）。外部公開時はこれらに認証必須。

**CORS の追加修正（2026-08-30）:** `api/main.py` の `CORSMiddleware.allow_methods` が `["GET"]` だったため、ブラウザからの `POST /llm/connections`（登録）や `DELETE`・`PUT`・`POST /llm/report/generate` がプリフライト `OPTIONS` で `400 Disallowed CORS method` になり、フロントには「APIサーバーに接続できません」としか出なかった。`allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]` に変更して解消。**API プロセスの再起動が必要**（`main.py` 変更なので）。

**生成エラーの理由が見えない件（2026-08-30）:** `generate_report()` は LLM 呼び出しの `LlmError` を握りつぶして `counts["failures"]=1` を返すだけだったため、UI には「レポート生成に失敗しました（ログを確認してください）」としか出なかった。`generate_report(..., raise_on_llm_error=True)` パラメータを追加し、`POST /llm/report/generate` はこれを渡す → 認証エラー・モデル名不正・base_url 不正などの**実際の理由**が `502 LLM呼び出しに失敗: <理由>` としてUIに返る。CLI（`generate-report`）は既定 `False` のまま（ログに出るので）。

**やり残し / 既知の制約:**
- `openai_compat` の `parse_structured` は `chat.completions.parse`（`response_format`=Pydantic）依存。互換サーバが未対応だと 400 → そのプロバイダでは `score-qualitative` 不可（`generate-report` は影響なし）。
- レート制限・ロックはプロセス内 global（`routes._report_gen_last_at` / `_REPORT_GEN_LOCK`）。複数ワーカー運用では効かない（個人ローカル前提 11.1解釈A なので許容）。
- `send_effort` 既定 false。推論モデルで効かせたいときだけ true。
- プロバイダ名は `llm_analyses` に列を持たない（モデル名で識別）。UIバッジは `model` のみ。

---

## 1. 目的とスコープ（ユーザーと確定済み）

現状 `uv run python -m autoscreener.cli generate-report` は **Anthropic の `claude-opus-5` 固定**で動く（`config/collection.yaml` の `llm.model` / `llm.effort` を手で編集すれば変えられるが、それだけ）。

ユーザーの要望は次の2点。**両方ともスコープに含む。**

| # | 要望 | 含意 |
|---|---|---|
| A | **UIで生成まで実行**できるようにする | 読み取り専用APIに書き込みエンドポイントを新設する。**課金を伴うHTTP導線を作る**（後述の原則を意図的に破る）。 |
| B | **Claude以外のプロバイダ**（NVIDIA NIM / ChatGPT / ローカルLLM）も選べる | `llm/client.py` の `anthropic.Anthropic` ハードコードを抽象化する。 |

### 方針の推奨（未確定事項1・2として人間に確認する）

- **B は `openai_compat` プロバイダ1本で束ねる**。NIM / ChatGPT / Ollama / vLLM / LM Studio / LiteLLM はいずれも OpenAI互換 `/v1/chat/completions` を持つため、`base_url` + APIキーを差し替えるだけで全部カバーできる。プロバイダごとの個別実装はしない。
- **A の課金ガードは「`confirm: true` 必須 + 短間隔レート制限（例: 30秒に1回）+ 同時実行ロック」**。APIには認証機構が一切ないため（下記参照）、ヘッダ認証まで入れるかは人間に確認する。

---

## 2. 現状アーキテクチャ（コード上の事実）

### 2.1 呼び出し経路

```
cli.py  generate-report (@app.command, src/autoscreener/cli.py:883)
  └─ batch/generate_report.py  generate_report()            ← score_date/top_n/config/client/today を引数で受ける
       └─ llm/client.py  LlmClient.complete_text()          ← ストリーミング1回。thinking=adaptive, effort
            └─ anthropic.Anthropic(api_key=...)             ← ここだけがネットワークに触れる
       └─ llm/report.py  report_system() / build_report_user_message()  ← プロンプト組み立て（純関数）
       └─ DB: llm_analyses に kind='daily_report', ticker_id=NULL で1行 INSERT
```

LLMを使うバッチは3つ。いずれも `LlmClient` 経由：

| CLIコマンド | バッチ | LlmClientのメソッド | Anthropic固有機能 |
|---|---|---|---|
| `generate-report` | `batch/generate_report.py` | `complete_text()`（ストリーミング） | `thinking={"type":"adaptive"}`, `output_config={"effort":...}` |
| `summarize-filings` | `batch/summarize_filings.py` | `complete_text()` | 同上 |
| `score-qualitative` | `batch/score_qualitative.py` | `parse_structured()`（`messages.parse` + `output_format`）, `submit_batch()`/`batches` | 構造化出力, **Batch API（料金50%）** |

### 2.2 主要ファイルと行番号

| ファイル | 要点 |
|---|---|
| `src/autoscreener/llm/client.py` | `LlmClient`。`__init__` で `anthropic.Anthropic(api_key=api_key)` を直接生成（:145）。`from_config()`（:147）で `enabled` とキーを検査し、無ければ `LlmDisabled`。`complete_text()`（:163）はストリーミング、`parse_structured()`（:200）は `messages.parse`、`submit_batch/wait_for_batch/collect_batch`（:238〜）は Batch API。 |
| `src/autoscreener/llm/errors.py` | 失敗分類。`LlmDisabled`（使わない構成、握ってOK）/ `LlmTransientFailure` / `LlmPermanentFailure` / `LlmRefusal` / `LlmTruncated` / `LlmInputTooLarge` / `LlmParseFailure`。`classify_exception()` が `anthropic.*` 例外を写す → **プロバイダ追加時はここも一般化が必要**（`openai.*` 例外を写す分岐、または各プロバイダが自前で分類して投げる）。 |
| `src/autoscreener/llm/__init__.py` | **不変条件の宣言**: このパッケージの出力はゲートにもスコアにも入らない。`tests/unit/test_llm_advisory_isolation.py` が `screening/` と `scoring/` がこのパッケージを import していないことを固定。 |
| `src/autoscreener/llm/prompts.py` | `cached_system()`（:155, `cache_control` を最終ブロックに付与）, `prompt_fingerprint(system, model, effort)`（:169, `cache_control` は指紋から除外）。 |
| `src/autoscreener/llm/report.py` | `report_system()`（:48）, `build_report_user_message()`（:52）, `report_source_refs()`（:78）。すべて純関数。 |
| `src/autoscreener/config.py` | `LlmConfig`（:517-556）。`enabled=True` / `model="claude-opus-5"` / `effort="high"`（`_known_effort` バリデータで `low|medium|high|xhigh|max` に制限, :551）/ `max_output_tokens=8000` / `max_input_chars=120_000` / `max_tickers_per_run=25` / `batch_*`。`Settings`（:593〜）は `.env` から読み、`anthropic_api_key`（:611）を保持。`load_llm_config()`（:703）が `collection.yaml` の `llm:` ブロックを検証。 |
| `config/collection.yaml` | `llm:` ブロック（:81〜）。`provider` フィールドは**まだ無い**。 |
| `src/autoscreener/cli.py` | `generate_report_cmd`（:883-899）は `--date` / `--top-n` / `--show` のみ。`generate_report()` を呼ぶだけ。`summarize_filings_cmd`（:831）, `score_qualitative_cmd`（:854）。 |
| `src/autoscreener/batch/generate_report.py` | `generate_report(*, score_date=None, top_n=10, config=None, client=None, today=None)`（:92）。**既に `config` と `client` を注入できる** → モデル/effort差し替えは `cfg.model_copy(update={...})` を渡すだけ。dedup は `prompt_fingerprint(system, cfg.model, cfg.effort)` で行い（:119, :134-143）、**モデルが違えば指紋が変わり別行が普通に追加される**（衝突しない）。 |
| `src/autoscreener/api/routes.py` | LLMは `GET /llm/report`（:2867）と `GET /llm/{ticker}`（:2912）の**読み取りのみ**。:2840 のコメントブロックに「18.6:APIは書かない」「HTTPリクエスト1本で課金が発生する導線を作らない」と明記。宣言順の注意（`/llm/report` を `/llm/{ticker}` より先に、:2864）。 |
| `src/autoscreener/api/schemas.py` | `LLM_DISCLAIMER`（:891, UI文言の唯一の出典）, `LlmUsageView`（:896）, `LlmReportResponse`（:968: `exists` / `score_date` / `as_of` / `model` / `effort` / `content` / `ranked_symbols` / `usage`）。 |
| `src/autoscreener/api/main.py` | FastAPIアプリ本体。`error_envelope` ミドルウェア（:25, CORSより内側に登録、500にCORSヘッダを付ける）。**認証・レート制限は無し**。CORSはlocalhost開発サーバのみ許可（:107、個人利用=11.1解釈A）。 |
| `src/autoscreener/api/dependencies.py` | `get_session`。 |
| `frontend/src/pages/LlmReportPage.tsx` | 「画面から生成はできない」と docstring に明記（:9-11）。未生成時は CLIコマンドを案内表示（:63-70）。モデル/effortは `data.model` / `data.effort` をバッジ表示（:76-79）。 |
| `frontend/src/api/client.ts` | `apiFetch<T>(path)` は **GET専用**（:32）。POSTヘルパは無い。`API_BASE` は `VITE_API_BASE_URL ?? "http://localhost:8000"`。 |
| `frontend/src/api/types.ts` | `LlmReportResponse` など（:711〜）。`LlmUsageView`（:656）。 |
| `.env.example` | `ANTHROPIC_API_KEY` を記載。 |

### 2.3 DB

`llm_analyses` テーブル（`src/autoscreener/db/models.py` の `LlmAnalysis`）は行ごとに `model` / `effort` / `prompt_fingerprint` / `usage`（JSON）/ `request_id` を保持。`daily_report` は `ticker_id=NULL`、部分ユニークインデックス `uq_llm_analyses_global`（`ticker_id IS NULL` 用）。**`provider` カラムは無い。** v1 ではマイグレーション不要（モデル名で識別、指紋で自然分離）。UIバッジにプロバイダを出したいなら別途 `provider` カラム追加のマイグレーションが要る（`alembic/versions/` に新リビジョン）。

---

## 3. 意図的に破る設計原則（重要）

このタスクは**明文化された原則を2つ破る**。着手前に人間が了承していること。

1. **原則18.6「APIは書かない」**（`routes.py:2840` のコメント、`schemas.py` の `LlmTickerAnalysisResponse` docstring 等）。
   → `POST /llm/report/generate` を新設して破る。

2. **「HTTPリクエスト1本で課金が発生する導線を作らない」**（`routes.py:2840`, `LlmReportPage.tsx:9-11`）。理由は「ブラウザのリロードや監視ツールのヘルスチェックが意図せず請求を積み上げうる」。
   → `confirm: true` 必須 + レート制限 + 同時実行ロックで**緩和**するが、ゼロにはできない。

**破ってはいけない原則（不変条件・死守）:**

- **LLM出力はゲート（`screening/`）にもスコア（`scoring/`）にも入らない。** `llm/__init__.py` と `test_llm_advisory_isolation.py`。新コードで `screening/` や `scoring/` から `llm/` を import しないこと。
- **数字はモデルに計算させない。** レポートは `scores` の算出済みの値を渡し、モデルの仕事は言語化のみ（`generate_report.py` / `report.py` docstring）。プロバイダを変えてもこの前提は変えない。
- **入力の黙った切り詰め禁止。** `guard_input_size()`（`client.py:84`）は `max_input_chars` 超過で `LlmInputTooLarge` を投げる。プロバイダ実装でもこのガードを通すこと。
- **`stop_reason` 検査を必ず通す。** `check_stop_reason()`（`client.py:101`）。拒否＝空文字、打ち切り＝途中まで書かれた完成品に見える文章、を保存しないため。OpenAI互換では `finish_reason`（`"length"` / `"content_filter"`）を同等に写す。
- **部分出力は保存しない。** 打ち切り時は行をINSERTしない。
- どの応答にも `advisory` と `disclaimer`（`LLM_DISCLAIMER`）を付ける。

---

## 4. 実装プラン

### パート1: プロバイダ抽象化（`src/autoscreener/llm/`, `config.py`, `config/collection.yaml`, `.env.example`）

**`LlmConfig` に追加:**
```yaml
llm:
  provider: anthropic        # anthropic | openai_compat
  model: claude-opus-5
  effort: high
  base_url: null             # openai_compat のエンドポイント（例: http://localhost:11434/v1, https://integrate.api.nvidia.com/v1）
  # api_key は .env: ANTHROPIC_API_KEY（anthropic）/ OPENAI_API_KEY または LLM_API_KEY（openai_compat）
```
- `provider` に `_known_provider` バリデータ（`_known_effort` に倣う）。
- `Settings` に `openai_api_key: str | None = None` を追加、`.env.example` にも追記。

**`client.py` を分割:**
- 共通IF（Protocol か ABC）:
  - `complete_text(*, system, user, max_tokens=None) -> LlmResult`
  - `parse_structured(*, system, user, output_model, max_tokens=None) -> tuple[Any, LlmUsage, str|None]`
  - `supports_batch: bool`（プロパティ）/ `submit_batch` 等は `supports_batch` が False なら `LlmPermanentFailure` を投げる
- `AnthropicProvider`: 現在の `LlmClient` の中身をほぼそのまま移設。
- `OpenAICompatProvider`（`openai` SDK, `base_url` + `api_key`）:
  - `complete_text`: `client.chat.completions.create(model=..., messages=[{system},{user}], stream=True, ...)` → チャンク集約。`system` は複数ブロックを `\n\n` で連結して1つの system メッセージにする（`cache_control` は落とす）。`effort` は `reasoning_effort` にマップ（対応しないモデルでは 400 になりうるので、`extra_body` 経由 + 例外時フォールバック、または config で `send_reasoning_effort: false` を持つ）。
  - `parse_structured`: `client.beta.chat.completions.parse(response_format=output_model, ...)`（OpenAI SDK の Pydantic ヘルパ）。互換サーバが未対応なら `response_format={"type":"json_schema","json_schema":{...}}` にフォールバック。パース不能は `LlmParseFailure`。
  - `supports_batch = False`。`score-qualitative` は「`openai_compat` では Batch 不可、逐次呼び出しにフォールバック（半額にならない旨をログ）」または「`anthropic` 必須」のどちらかにする（未確定事項ではないが、実装者判断でログ＋逐次が無難）。
- `classify_exception()`: `openai.APIError` 系の分岐を追加、またはプロバイダ側で分類して `LlmError` サブクラスを投げる。
- `LlmClient.from_config()` 相当を**ファクトリ関数** `build_provider(cfg) -> Provider` にする。`enabled=False` / キー未設定は従来どおり `LlmDisabled`。

**`generate_report.py` / `summarize_filings.py` / `score_qualitative.py`:**
- `client` 引数の型を新IFに変更。`LlmClient.from_config(cfg)` の呼び出しを `build_provider(cfg)` に置換。ロジックは原則変えない。

**`prompt_fingerprint`:** `provider` も指紋に含めるか要検討。含めると「同じモデル名でもプロバイダが違えば別行」になり安全。`prompts.py:169` のシグネチャに `provider` を足すのが素直（呼び出し3か所）。

### パート2: 書き込みAPI（`routes.py`, `schemas.py`）

**新規: `POST /llm/report/generate`**
```python
class GenerateReportRequest(BaseModel):
    score_date: datetime.date | None = None
    top_n: int = Field(default=10, ge=1, le=50)
    provider: str | None = None      # None なら config の既定
    model: str | None = None
    effort: str | None = None
    confirm: bool = False            # False なら 400（誤爆防止）

class GenerateReportResult(BaseModel):
    report: LlmReportResponse        # 生成結果をそのまま返す
    created: bool                    # 新規生成したか（既存ヒットなら False）
```
- `confirm != True` → `HTTPException(400, "confirm=true が必要です（課金が発生します）")`
- **同時実行ロック**: モジュールレベルの `threading.Lock`（`asyncio` なら `anyio` ロック）。取得できなければ `HTTPException(409, "別のレポート生成が進行中です")`。
- **レート制限**: 最終生成時刻をモジュール変数で保持し、`min_interval_seconds`（例30）以内の再要求は `HTTPException(429, Retry-After)`。`edgar` の `throttle_cooldown_seconds` の思想に倣う。
- `provider/model/effort` を `load_llm_config()` の結果に `model_copy(update=...)` で被せて `generate_report(score_date=..., top_n=..., config=overridden_cfg)` を呼ぶ。`effort` は `_known_effort` の集合でバリデート、`provider` も同様。
- 生成後、既存の `get_llm_report` と同じ整形で `LlmReportResponse` を作って返す（整形部分を private 関数に切り出して共有）。
- `LlmDisabled` は 200 + `report.exists=False` 相当ではなく、ここでは **409 or 422 で「LLM未設定」** を明示（UIがボタンを出している以上、無言成功は混乱する）。
- **`error_envelope`（main.py:25）が `Exception` を最外で握る**ので、`LlmError` を投げても500 JSONになる。意味のあるHTTPステータスにしたいなら `routes.py` 側で `LlmError` を catch して `HTTPException` に変換する。

**新規: `GET /llm/providers`（任意だがUIのために推奨）**
```python
class LlmProviderInfo(BaseModel):
    provider: str
    configured: bool          # APIキー等が揃っているか
    default_model: str
    suggested_models: list[str]
    efforts: list[str] = ["low","medium","high","xhigh","max"]
```
- `anthropic`: `suggested_models = ["claude-opus-5","claude-sonnet-5","claude-haiku-4-5-20251001"]`
- `openai_compat`: `base_url` が設定済みなら `configured=True`、`suggested_models` は config から（自由入力もUIで許可）。

### パート3: フロント（`LlmReportPage.tsx`, `api/client.ts`, `api/types.ts`）

- `client.ts`: `apiPost<T>(path, body)` を追加（`apiFetch` と同じエラー整形。`res.status` に応じて 400/409/429 のメッセージを出す）。
- `types.ts`: `GenerateReportRequest` / `GenerateReportResult` / `LlmProviderInfo` を追加。
- `LlmReportPage.tsx`:
  - 未生成ブロック（現行 :63-70）に「生成パネル」を追加: provider セレクト → model セレクト（`GET /llm/providers` の `suggested_models`、`openai_compat` は自由入力も可）→ effort セレクト → 「生成」ボタン。
  - ボタン押下で `window.confirm("このモデルでレポートを生成します。APIの課金が発生します。よろしいですか？")` → OKで `apiPost("/llm/report/generate", { ..., confirm: true })`。
  - 生成中は spinner + ボタン disabled。成功で `setData(result.report)`。429/409 はそのままメッセージ表示。
  - 既存の免責文（`data.disclaimer` / `LLM_DISCLAIMER`）は据え置き。

### パート4: テスト

| ファイル | 追加内容 |
|---|---|
| `tests/unit/test_llm_client.py` | `OpenAICompatProvider` を fake client（`openai` SDK をモック）で検証: `complete_text` のチャンク集約、`finish_reason=="length"` → `LlmTruncated`、`content_filter` → `LlmRefusal` 相当、`guard_input_size` 経由の `LlmInputTooLarge`。`build_provider(cfg)` の分岐。 |
| `tests/unit/test_llm_batches.py` | `openai_compat` で `supports_batch=False` → `score-qualitative` が逐次にフォールバック（またはエラー）する経路。 |
| `tests/unit/test_api_llm.py` | `POST /llm/report/generate`: `confirm` 無し→400、正常（fake provider 注入）→ `created=True` + 本文、直後の再POST→429、同時→409、`LlmDisabled`→409/422。`generate_report` に fake client を渡せる既存の口を使う。 |
| `tests/unit/test_llm_advisory_isolation.py` | **変更不要・必ずパスさせる**。`screening/` `scoring/` が `llm/` を import しないこと。 |
| `tests/unit/test_llm_prompts.py` | `prompt_fingerprint` に `provider` を足したならその反映。 |

`generate_report()` にテスト用 client を注入する既存パターン（`client` 引数, `generate_report.py:97`, `client.py:143` のコメント）をそのまま使えば実APIを叩かずに検証できる。

---

## 5. 見積り

| パート | 変更ファイル | 規模 |
|---|---|---|
| 1 プロバイダ抽象化 | `llm/client.py`, `llm/errors.py`, `llm/prompts.py`, `config.py`, `config/collection.yaml`, `.env.example`, `batch/{generate_report,summarize_filings,score_qualitative}.py` | 中〜大 |
| 2 書き込みAPI | `api/routes.py`, `api/schemas.py` | 中 |
| 3 フロント | `frontend/src/pages/LlmReportPage.tsx`, `frontend/src/api/client.ts`, `frontend/src/api/types.ts` | 小〜中 |
| 4 テスト | 上記4テストファイル | 中 |

DBマイグレーション: v1では不要。UIにプロバイダ名バッジを出すなら `llm_analyses.provider` カラム追加の alembic リビジョンを別途。

依存追加: `openai` SDK（`pyproject.toml` / `uv.lock`）。

---

## 6. 未確定事項（着手前に人間へ確認）

1. **プロバイダは `openai_compat` 1本で束ねる方針でよいか。**
   （NIM / ChatGPT / Ollama / vLLM / LM Studio / LiteLLM を個別実装せず、OpenAI互換エンドポイント + `base_url` 差し替えで対応する。Anthropic native は別プロバイダとして残す。）

2. **課金ガードの強度。**
   - 案A（推奨）: `confirm: true` 必須 + 30秒レート制限 + 同時実行ロック。認証は追加しない（現状どおり個人ローカル利用前提）。
   - 案B: 案A に加えて `.env` の専用トークンによるヘッダ認証（`X-Admin-Token` 等）を書き込みエンドポイントだけに課す。

3. （小）`score-qualitative` を `openai_compat` で使うとき: **逐次呼び出しフォールバック**（半額不可・警告ログ）か、**`anthropic` 必須エラー**か。

4. （小）`prompt_fingerprint` に `provider` を含めるか（含める推奨。同名モデルの取り違え防止）。

5. （小）UIにプロバイダ名を出すために `llm_analyses.provider` カラム（＝マイグレーション）を追加するか。v1はモデル名表示のみで見送り可。

---

## 7. 着手順（推奨）

1. パート1のうち `config.py` + `collection.yaml` + `LlmConfig` バリデータ（`provider` / `base_url`）→ テストが緑のまま通ることを確認。
2. `client.py` の IF 切り出し + `AnthropicProvider` へ現行ロジック移設（**挙動を変えない**リファクタ）→ 既存の全LLMテスト緑。
3. `OpenAICompatProvider` 実装 + `test_llm_client.py` 追加。
4. パート2の `POST /llm/report/generate` + `test_api_llm.py`。
5. パート3のフロント。
6. `GET /llm/providers` とUIのセレクト連携。
7. `uv run pytest`（全体）+ `test_llm_advisory_isolation.py` が緑であること、`uv run python -m autoscreener.cli generate-report` が従来どおり（provider未指定でAnthropic）動くことを確認。

---

## 8. 参考: 変更してはいけない既存の振る舞い

- `generate-report` を**引数なし**で叩いたときの既定は `claude-opus-5` / `effort=high` / Anthropic のまま（後方互換）。
- `ANTHROPIC_API_KEY` 未設定時に CLI が「0件で正常終了」する挙動（`LlmDisabled` を握る、`generate_report.py:110-115`）。
- `GET /llm/report` が未生成時に `exists=False` で **200** を返す（404にしない）。
- LLM出力の `llm_analyses` 隔離と `advisory`/`disclaimer` の付与。
