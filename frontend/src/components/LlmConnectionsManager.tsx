import { useEffect, useState } from "react";
import {
  activateLlmConnection,
  createLlmConnection,
  deactivateLlmConnections,
  deleteLlmConnection,
  fetchLlmConnections,
  fetchLlmSettings,
  updateLlmConnection,
} from "../api/client";
import type { LlmConnection, LlmSettings } from "../api/types";

/** K-9(docs/ui_llm_provider_selection_2026-08-30.md):名前付きLLM接続プロファイルの管理。
 *
 * provider / base_url / model / APIキー を名前を付けて何件でも保存し、1件を
 * **アクティブ** にする。アクティブな行が collection.yaml / .env の上に重なり、
 * UI と CLL の両方(`generate-report` 等)に効く。アクティブが無ければ既定のまま。
 *
 * **APIキーの本体はサーバから返らない。** 一覧では「設定済み」表示のみ。編集時も
 * 空欄のまま置き、変更するときだけ入力する。
 *
 * ここで作る設定はゲートにもスコアにも影響しない——LLMの宛先を選ぶだけ。
 */

const PROVIDERS = ["anthropic", "openai_compat"] as const;
const EFFORTS = ["", "low", "medium", "high", "xhigh", "max"] as const;

interface FormState {
  name: string;
  provider: string;
  baseUrl: string;
  model: string;
  effort: string;
  sendEffort: boolean;
  apiKey: string;
}

const EMPTY_FORM: FormState = {
  name: "",
  provider: "anthropic",
  baseUrl: "",
  model: "",
  effort: "",
  sendEffort: false,
  apiKey: "",
};

export function LlmConnectionsManager({ onChanged }: { onChanged?: () => void }) {
  const [connections, setConnections] = useState<LlmConnection[]>([]);
  const [settings, setSettings] = useState<LlmSettings | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  function reload() {
    return Promise.all([fetchLlmConnections(), fetchLlmSettings()])
      .then(([c, s]) => {
        setConnections(c.connections);
        setSettings(s);
      })
      .catch((e: Error) => setError(e.message));
  }

  useEffect(() => {
    void reload();
  }, []);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function openNew() {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setFormOpen(true);
    setNote(null);
    setError(null);
  }

  function openEdit(c: LlmConnection) {
    setForm({
      name: c.name,
      provider: c.provider,
      baseUrl: c.base_url ?? "",
      model: c.model ?? "",
      effort: c.effort ?? "",
      sendEffort: c.send_effort,
      apiKey: "",
    });
    setEditingId(c.id);
    setFormOpen(true);
    setNote(null);
    setError(null);
  }

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await fn();
      await reload();
      setNote(ok);
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function submitForm(activate: boolean) {
    const common = {
      provider: form.provider,
      base_url: form.baseUrl,
      model: form.model,
      effort: form.effort,
      send_effort: form.sendEffort,
    };
    if (editingId != null) {
      const body = { ...common, name: form.name, ...(form.apiKey.trim() ? { api_key: form.apiKey.trim() } : {}) };
      void run(async () => {
        await updateLlmConnection(editingId, body);
        if (activate) await activateLlmConnection(editingId);
        setFormOpen(false);
      }, "保存しました。");
    } else {
      void run(async () => {
        await createLlmConnection({
          ...common,
          name: form.name.trim(),
          api_key: form.apiKey.trim() || null,
          activate,
        });
        setFormOpen(false);
      }, activate ? "作成してアクティブにしました。" : "作成しました。");
    }
  }

  return (
    <div>
      <p className="ticker-meta">
        接続プロファイルを名前を付けて保存し、1件をアクティブにします。アクティブな設定は
        collection.yaml / .env の上に重なり、UI と CLL(<code>generate-report</code> 等)の両方に効きます。
        <code>openai_compat</code> は OpenAI互換API(ChatGPT / NVIDIA NIM / Ollama / vLLM / LiteLLM)。
      </p>

      {settings && (
        <p className="ticker-meta">
          現在の実効設定:
          <span className="th-badge">
            {settings.active_connection_name ?? "プロファイル未使用(collection.yaml / .env)"}
          </span>{" "}
          {settings.provider} / {settings.model}
          {settings.base_url ? ` / ${settings.base_url}` : ""}
        </p>
      )}

      <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>名前</th>
            <th>プロバイダ</th>
            <th>base_url</th>
            <th>モデル</th>
            <th>APIキー</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {connections.length === 0 && (
            <tr>
              <td colSpan={6} className="ticker-meta">
                まだプロファイルがありません。「新規追加」から作成してください。
              </td>
            </tr>
          )}
          {connections.map((c) => (
            <tr key={c.id}>
              <td>
                {c.is_active && <span className="th-badge">アクティブ</span>} {c.name}
              </td>
              <td>{c.provider}</td>
              <td>{c.base_url ?? "—"}</td>
              <td>{c.model ?? "(既定)"}</td>
              <td>{c.api_key_set ? "設定済み" : "—"}</td>
              <td>
                {!c.is_active && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void run(() => activateLlmConnection(c.id), `「${c.name}」をアクティブにしました。`)}
                  >
                    有効化
                  </button>
                )}{" "}
                <button type="button" disabled={busy} onClick={() => openEdit(c)}>
                  編集
                </button>{" "}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    if (window.confirm(`プロファイル「${c.name}」を削除しますか?`)) {
                      void run(() => deleteLlmConnection(c.id), `「${c.name}」を削除しました。`);
                    }
                  }}
                >
                  削除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>

      <div className="filters">
        {!formOpen && (
          <button type="button" disabled={busy} onClick={openNew}>
            新規追加
          </button>
        )}
        {settings?.active_connection_id != null && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void run(deactivateLlmConnections, "アクティブを解除しました(既定に戻ります)。")}
          >
            アクティブを解除
          </button>
        )}
      </div>

      {formOpen && (
        <div className="dd-section">
          <h4>{editingId != null ? "プロファイルを編集" : "プロファイルを新規追加"}</h4>
          <div className="filters">
            <label>
              名前
              <input type="text" value={form.name} onChange={(e) => set("name", e.target.value)} />
            </label>
            <label>
              プロバイダ
              <select value={form.provider} onChange={(e) => set("provider", e.target.value)}>
                {PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </label>
            <label>
              base_url
              <input
                type="text"
                value={form.baseUrl}
                placeholder="(既定: OpenAI本家 / anthropic では未使用)"
                onChange={(e) => set("baseUrl", e.target.value)}
              />
            </label>
            <label>
              モデル
              <input
                type="text"
                value={form.model}
                placeholder="(空なら collection.yaml の既定)"
                onChange={(e) => set("model", e.target.value)}
              />
            </label>
            <label>
              effort
              <select value={form.effort} onChange={(e) => set("effort", e.target.value)}>
                {EFFORTS.map((ef) => (
                  <option key={ef || "default"} value={ef}>
                    {ef || "(既定)"}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <input
                type="checkbox"
                checked={form.sendEffort}
                onChange={(e) => set("sendEffort", e.target.checked)}
              />
              effort を openai_compat に reasoning_effort として送る
            </label>
            <label>
              APIキー
              <input
                type="password"
                value={form.apiKey}
                placeholder={
                  editingId != null && connections.find((c) => c.id === editingId)?.api_key_set
                    ? "設定済み(変更する場合のみ入力)"
                    : "ローカルLLMなら任意のダミー文字列"
                }
                onChange={(e) => set("apiKey", e.target.value)}
              />
            </label>
          </div>
          <div className="filters">
            <button type="button" disabled={busy || !form.name.trim()} onClick={() => submitForm(false)}>
              {editingId != null ? "保存" : "作成"}
            </button>
            <button type="button" disabled={busy || !form.name.trim()} onClick={() => submitForm(true)}>
              {editingId != null ? "保存してアクティブに" : "作成してアクティブに"}
            </button>
            <button type="button" disabled={busy} onClick={() => setFormOpen(false)}>
              キャンセル
            </button>
          </div>
        </div>
      )}

      {note && <p className="ticker-meta">{note}</p>}
      {error && <p className="error">エラー: {error}</p>}
    </div>
  );
}
