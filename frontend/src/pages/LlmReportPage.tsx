import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  fetchLlmProviders,
  fetchLlmReport,
  fetchScoreDates,
  generateLlmReport,
} from "../api/client";
import type {
  LlmProviderInfo,
  LlmReportResponse,
  ScoreDatesResponse,
} from "../api/types";
import { LlmConnectionsManager } from "../components/LlmConnectionsManager";
import { LlmMarkdown } from "../components/LlmMarkdown";

/** K-9:当日ランキングの説明文(生成AI・参考)。
 *
 * **生成はこの画面から実行できる**(ui_llm_provider_selection_2026-08-30.md)。
 * ただし課金が発生するので、モデル/プロバイダを選んだうえで確認ダイアログを
 * 挟み、`confirm: true` を明示的に送る。サーバ側でも 30 秒のレート制限と
 * 同時実行ロックがかかる。読み取り(表示)は従来どおり無条件。
 *
 * 未生成は正常な状態なので、エラーではなく生成フォームを出す。
 */
export function LlmReportPage() {
  const [date, setDate] = useState<string>("");
  const [dates, setDates] = useState<ScoreDatesResponse | null>(null);
  const [data, setData] = useState<LlmReportResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [providers, setProviders] = useState<LlmProviderInfo[]>([]);
  const [provider, setProvider] = useState<string>("");
  const [model, setModel] = useState<string>("");
  const [effort, setEffort] = useState<string>("high");
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);
  const [genNote, setGenNote] = useState<string | null>(null);

  function reloadProviders() {
    return fetchLlmProviders()
      .then((r) => {
        setProviders(r.providers);
        const cur = r.providers.find((p) => p.provider === r.current) ?? r.providers[0];
        if (cur) applyProviderDefaults(cur);
      })
      .catch(() => setProviders([]));
  }

  useEffect(() => {
    fetchScoreDates(30)
      .then(setDates)
      .catch(() => setDates(null));
    void reloadProviders();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** プロバイダを選んだとき、そのプロバイダの既定モデル/effortに寄せる。
   *  派生 state を effect で同期すると再レンダリングが連鎖するので、選択イベント側で行う。 */
  function applyProviderDefaults(p: LlmProviderInfo) {
    setProvider(p.provider);
    setModel(p.default_model);
    setEffort(p.efforts.includes("high") ? "high" : (p.efforts[0] ?? "high"));
  }

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchLlmReport(date || undefined)
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [date]);

  const activeProvider = useMemo(
    () => providers.find((p) => p.provider === provider) ?? null,
    [providers, provider],
  );

  async function handleGenerate() {
    if (!activeProvider) return;
    const label = `${activeProvider.provider} / ${model || activeProvider.default_model}`;
    if (
      !window.confirm(
        `${label} でレポートを生成します。\nLLM API の課金が発生します。よろしいですか?`,
      )
    ) {
      return;
    }
    setGenerating(true);
    setGenError(null);
    setGenNote(null);
    try {
      const result = await generateLlmReport({
        score_date: date || null,
        provider: activeProvider.provider,
        model: model || activeProvider.default_model,
        effort,
        confirm: true,
      });
      setData(result.report);
      setGenNote(
        result.created
          ? "生成しました。"
          : "同じ指示文・同じモデルの既存レポートを表示しています(新規生成はしていません)。",
      );
    } catch (e) {
      setGenError((e as Error).message);
    } finally {
      setGenerating(false);
    }
  }

  const canGenerate = activeProvider?.configured && !generating && !loading;

  return (
    <div>
      <h2>日次レポート(生成AI・参考)</h2>
      <p className="ticker-meta">
        その日のランキング上位について、定量モデルが出した数字を言語化したものです。
        数値はすべて <Link to="/">ランキング</Link> と同じ `scores` の値で、モデルに再計算はさせていません。
      </p>
      {data && <p className="llm-disclaimer">{data.disclaimer}</p>}

      <div className="filters">
        <label>
          対象日
          <select value={date} onChange={(e) => setDate(e.target.value)}>
            <option value="">最新</option>
            {(dates?.dates ?? []).map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
      </div>

      {loading && <p>読み込み中...</p>}
      {error && <p className="error">エラー: {error}</p>}

      {data && !loading && !data.exists && (
        <p className="ticker-meta">
          {date ? `${date} の` : ""}レポートはまだ作られていません。下のフォームから生成するか、次を実行します:
          <br />
          <code>uv run python -m autoscreener.cli generate-report{date ? ` --date ${date}` : ""}</code>
        </p>
      )}

      {data && data.exists && (
        <div className="dd-section">
          <p className="ticker-meta">
            対象日 {data.score_date ?? "—"} ・ 生成 {data.as_of ?? "—"}
            <span className="th-badge">
              {data.model} / effort {data.effort}
            </span>
          </p>
          <LlmMarkdown content={data.content ?? ""} />

          {data.ranked_symbols.length > 0 && (
            <p className="llm-sources">
              対象銘柄:
              {data.ranked_symbols.map((symbol, i) => (
                <span key={symbol}>
                  {i > 0 && " / "}
                  <Link to={`/candidates/${symbol}`}>{symbol}</Link>
                </span>
              ))}
            </p>
          )}

          {data.usage && (
            <p className="pipeline-history-note">
              トークン: 入力 {data.usage.input_tokens.toLocaleString()} ・ キャッシュ読み{" "}
              {data.usage.cache_read_tokens.toLocaleString()} ・ 出力 {data.usage.output_tokens.toLocaleString()}
              {data.usage.cache_read_tokens === 0 &&
                " (キャッシュ未ヒット。同じ指示文で繰り返し生成しているなら、プロンプトの前方一致が壊れている可能性があります)"}
            </p>
          )}
        </div>
      )}

      <details className="dd-section" open={!!data && !data.exists}>
        <summary>レポートを生成する(課金が発生します)</summary>
        <p className="ticker-meta">
          使うモデルを選んで生成します。ここで作る文章はゲートにもスコアにも入らず、表示・下読み専用です。
          NVIDIA NIM / ChatGPT / ローカルLLM を使うには、サーバの <code>.env</code> と
          <code>config/collection.yaml</code> の <code>llm.provider</code> / <code>llm.base_url</code> を設定してください。
        </p>
        <div className="filters">
          <label>
            プロバイダ
            <select
              value={provider}
              onChange={(e) => {
                const next = providers.find((p) => p.provider === e.target.value);
                if (next) applyProviderDefaults(next);
              }}
            >
              {providers.map((p) => (
                <option key={p.provider} value={p.provider}>
                  {p.provider}
                  {p.configured ? "" : "(未設定)"}
                </option>
              ))}
            </select>
          </label>
          <label>
            モデル
            <input
              list="llm-model-options"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={activeProvider?.default_model ?? ""}
            />
            <datalist id="llm-model-options">
              {(activeProvider?.suggested_models ?? []).map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </label>
          <label>
            effort
            <select value={effort} onChange={(e) => setEffort(e.target.value)}>
              {(activeProvider?.efforts ?? ["low", "medium", "high", "xhigh", "max"]).map((ef) => (
                <option key={ef} value={ef}>
                  {ef}
                </option>
              ))}
            </select>
          </label>
          <button type="button" disabled={!canGenerate} onClick={handleGenerate}>
            {generating ? "生成中..." : "生成"}
          </button>
        </div>
        {activeProvider && !activeProvider.configured && (
          <p className="ticker-meta">
            このプロバイダはAPIキーが未設定のため使えません。下の「LLM接続プロファイル」で
            APIキー付きのプロファイルを作ってアクティブにするか、サーバの .env を設定してください。
          </p>
        )}
        {genNote && <p className="ticker-meta">{genNote}</p>}
        {genError && <p className="error">生成エラー: {genError}</p>}
      </details>

      <details className="dd-section">
        <summary>LLM接続プロファイル(provider / URL / モデル / APIキーを名前を付けて保存)</summary>
        <LlmConnectionsManager onChanged={reloadProviders} />
      </details>
    </div>
  );
}
