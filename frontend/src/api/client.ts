import type {
  AlertsResponse,
  BacktestSummary,
  CalendarResponse,
  CandidateDetail,
  CandidateListResponse,
  ExcludedListResponse,
  FilingListResponse,
  PeerResponse,
  BenchmarkReferenceResponse,
  FinancialHistoryResponse,
  FxRateResponse,
  GenerateReportRequest,
  GenerateReportResult,
  LlmConnection,
  LlmConnectionCreate,
  LlmConnectionUpdate,
  LlmConnectionsResponse,
  LlmProvidersResponse,
  LlmReportResponse,
  LlmSettings,
  LlmTickerAnalysisResponse,
  MacroResponse,
  PipelineRunDetail,
  PipelineRunListResponse,
  PositionsResponse,
  ResearchNoteResponse,
  ScoreDatesResponse,
  UniverseStatusResponse,
  WatchlistResponse,
  InvestmentIntelligenceResponse,
  ReverseValuationResponse,
  DataCoverageResponse,
  ModelV5ObjectivesResponse,
  ModelV5Run,
  ModelV5ScoreDetail,
  ModelV5ScoreListResponse,
  ModelV5ValidationStatus,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function apiFetch<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`);
  } catch {
    // fetch() が例外を投げるのは、応答が返らなかったときか、ブラウザが応答を
    // ブロックしたとき。ブラウザは理由を教えてくれない(セキュリティ上わざと)。
    // 素の "Failed to fetch" のままでは何も分からないので、確認すべきことを示す。
    throw new ApiError(
      0,
      `APIサーバー(${API_BASE})に接続できません。次を確認してください:
` +
        `1. APIが起動しているか — uv run uvicorn autoscreener.api.main:app --port 8000
` +
        `2. ${API_BASE}/ready をブラウザで開けるか(DBとスキーマの整合まで確認できます)
` +
        `3. マイグレーション後にAPIプロセスを再起動したか`,
    );
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? res.statusText);
  }
  return res.json() as Promise<T>;
}

/**
 * POST ヘルパ。**API層で書き込むのは K-9 のレポート生成だけ**——他は読み取り
 * 専用(18.6)。`apiFetch` と同じエラー整形にして、400/409/429 の detail を
 * そのまま画面に出せるようにする。
 */
async function apiSend<T>(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  payload?: unknown,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: payload === undefined ? undefined : { "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
  } catch {
    throw new ApiError(
      0,
      `APIサーバー(${API_BASE})に接続できません。APIが起動しているか確認してください。`,
    );
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

const apiPost = <T,>(path: string, payload?: unknown) => apiSend<T>("POST", path, payload);
const apiPut = <T,>(path: string, payload: unknown) => apiSend<T>("PUT", path, payload);
const apiDelete = <T,>(path: string) => apiSend<T>("DELETE", path);

export interface TargetParams {
  /** 目標達成までの年数(1〜15)。未指定なら設定値(既定7年)。 */
  horizonYears?: number;
  /** 目標倍率(1.5〜100)。未指定なら設定値(既定10倍)。 */
  targetMoic?: number;
}

export interface ListCandidatesParams extends TargetParams {
  date?: string;
  sector?: string;
  minMarketCap?: number;
  maxMarketCap?: number;
  limit?: number;
  offset?: number;
  /** 30.2.1:trueのとき取扱可否リストで"tradable"の銘柄だけを返す。 */
  tradableOnly?: boolean;
}

export function fetchCandidates(params: ListCandidatesParams = {}): Promise<CandidateListResponse> {
  const q = new URLSearchParams();
  if (params.date) q.set("date", params.date);
  if (params.sector) q.set("sector", params.sector);
  if (params.minMarketCap != null) q.set("min_market_cap", String(params.minMarketCap));
  if (params.maxMarketCap != null) q.set("max_market_cap", String(params.maxMarketCap));
  if (params.horizonYears != null) q.set("horizon_years", String(params.horizonYears));
  if (params.targetMoic != null) q.set("target_moic", String(params.targetMoic));
  if (params.tradableOnly) q.set("tradable_only", "true");
  q.set("limit", String(params.limit ?? 50));
  q.set("offset", String(params.offset ?? 0));
  return apiFetch(`/api/v1/candidates?${q.toString()}`);
}

export function fetchCandidateDetail(ticker: string, target: TargetParams = {}): Promise<CandidateDetail> {
  const q = new URLSearchParams();
  if (target.horizonYears != null) q.set("horizon_years", String(target.horizonYears));
  if (target.targetMoic != null) q.set("target_moic", String(target.targetMoic));
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return apiFetch(`/api/v1/candidates/${encodeURIComponent(ticker)}${suffix}`);
}

/** J-2:財務推移(売上・粗利率・CF・現金・株式数・ランウェイ・F-score内訳)。表示専用。 */
export function fetchCandidateFinancials(ticker: string): Promise<FinancialHistoryResponse> {
  return apiFetch(`/api/v1/candidates/${encodeURIComponent(ticker)}/financials`);
}

const intelligencePath = (ticker: string, section: string) =>
  `/api/v1/candidates/${encodeURIComponent(ticker)}/${section}`;

export function fetchReverseValuation(ticker: string, horizonYears = 7): Promise<ReverseValuationResponse> {
  return apiFetch(`${intelligencePath(ticker, "reverse-valuation")}?horizon_years=${horizonYears}`);
}

export function fetchInvestmentIntelligence(ticker: string, section: string): Promise<InvestmentIntelligenceResponse> {
  return apiFetch(intelligencePath(ticker, section));
}

export function fetchRiskSizing(ticker: string, realizedVol?: number | null, evidenceGrade = "C"): Promise<InvestmentIntelligenceResponse> {
  const q = new URLSearchParams({ ticker, evidence_grade: evidenceGrade });
  if (realizedVol != null) q.set("realized_vol", String(realizedVol));
  return apiFetch(`/api/v1/positions/risk-sizing?${q.toString()}`);
}

export function fetchJpyReturn(ticker: string, usdMoic: number, entryUsdJpy: number, exitUsdJpy: number): Promise<InvestmentIntelligenceResponse> {
  const q = new URLSearchParams({ ticker, usd_moic: String(usdMoic), entry_usdjpy: String(entryUsdJpy), exit_usdjpy: String(exitUsdJpy) });
  return apiFetch(`/api/v1/positions/jpy-return?${q.toString()}`);
}

export function fetchDataCoverage(): Promise<DataCoverageResponse> {
  return apiFetch(`/api/v1/data-coverage`);
}

export function fetchCandidatePeers(ticker: string): Promise<PeerResponse> {
  return apiFetch(`/api/v1/candidates/${encodeURIComponent(ticker)}/peers`);
}

export function fetchBenchmarkReference(horizonYears = 7): Promise<BenchmarkReferenceResponse> {
  return apiFetch(`/api/v1/benchmark/reference?horizon_years=${horizonYears}`);
}

export function fetchUniverseStatus(): Promise<UniverseStatusResponse> {
  return apiFetch(`/api/v1/universe/status`);
}

/** 14.15:日次ジョブの実行履歴(直近N回)。 */
export function fetchPipelineRuns(limit = 14): Promise<PipelineRunListResponse> {
  return apiFetch(`/api/v1/pipeline/runs?limit=${limit}`);
}

/** `runId` に "latest" を渡すと最新実行(初回描画で2往復させないため)。 */
export function fetchPipelineRun(runId: string): Promise<PipelineRunDetail> {
  return apiFetch(`/api/v1/pipeline/runs/${encodeURIComponent(runId)}`);
}

/** J-6:近いカタリスト(次回決算日・検証日)を近い順に。 */
export function fetchCalendar(days = 30): Promise<CalendarResponse> {
  return apiFetch(`/api/v1/calendar?days=${days}`);
}

/** J-10:円換算表示のための USD/JPY レート(表示用のみ)。 */
export function fetchUsdJpy(): Promise<FxRateResponse> {
  return apiFetch(`/api/v1/fx/usdjpy`);
}

export interface FetchExcludedParams extends TargetParams {
  reason?: string;
  limit?: number;
  offset?: number;
}

/**
 * 除外銘柄一覧。29章から**目標に依存する**——規模の上限が目標倍率の関数に
 * なったため、「7年で10倍には大きすぎる」銘柄は目標を緩めると候補側へ移る。
 */
export function fetchExcluded(params: FetchExcludedParams = {}): Promise<ExcludedListResponse> {
  const q = new URLSearchParams();
  if (params.reason) q.set("reason", params.reason);
  if (params.horizonYears != null) q.set("horizon_years", String(params.horizonYears));
  if (params.targetMoic != null) q.set("target_moic", String(params.targetMoic));
  q.set("limit", String(params.limit ?? 50));
  q.set("offset", String(params.offset ?? 0));
  return apiFetch(`/api/v1/excluded?${q.toString()}`);
}

export function fetchScoreDates(limit = 30): Promise<ScoreDatesResponse> {
  return apiFetch(`/api/v1/scores/dates?limit=${limit}`);
}

export interface FetchWatchlistParams extends TargetParams {
  reason?: string;
  gate?: string;
  limit?: number;
  offset?: number;
}

export function fetchWatchlist(params: FetchWatchlistParams = {}): Promise<WatchlistResponse> {
  const q = new URLSearchParams();
  if (params.reason) q.set("reason", params.reason);
  if (params.gate) q.set("gate", params.gate);
  // 29章:規模の上限が目標倍率の関数になったため、Tier 2 の分類も目標に依存する。
  if (params.horizonYears != null) q.set("horizon_years", String(params.horizonYears));
  if (params.targetMoic != null) q.set("target_moic", String(params.targetMoic));
  q.set("limit", String(params.limit ?? 50));
  q.set("offset", String(params.offset ?? 0));
  return apiFetch(`/api/v1/watchlist?${q.toString()}`);
}

export function fetchLatestBacktest(): Promise<BacktestSummary> {
  return apiFetch(`/api/v1/backtest/latest`);
}

/** 30.4.3:銘柄詳細の「提出書類とレッドフラグ」節が使う一次情報一覧。 */
export function fetchFilings(ticker: string, limit = 50): Promise<FilingListResponse> {
  return apiFetch(`/api/v1/filings/${encodeURIComponent(ticker)}?limit=${limit}`);
}

/** 30.8:マクロ(FRED)3系列。FRED_API_KEY未設定でも200で`enabled:false`が返る。 */
export function fetchMacro(): Promise<MacroResponse> {
  return apiFetch(`/api/v1/macro`);
}

export interface FetchAlertsParams {
  days?: number;
  severity?: "blocking" | "warning" | "info";
  includeAcknowledged?: boolean;
  limit?: number;
}

/** 30.7.5:直近アラート。既定では未解消のものだけを返す。 */
export function fetchAlerts(params: FetchAlertsParams = {}): Promise<AlertsResponse> {
  const q = new URLSearchParams();
  q.set("days", String(params.days ?? 30));
  if (params.severity) q.set("severity", params.severity);
  if (params.includeAcknowledged) q.set("include_acknowledged", "true");
  q.set("limit", String(params.limit ?? 100));
  return apiFetch(`/api/v1/alerts?${q.toString()}`);
}

/** 30.7.5:保有一覧とポートフォリオ集計。positions.yamlが無ければ空配列。 */
export function fetchPositions(): Promise<PositionsResponse> {
  return apiFetch(`/api/v1/positions`);
}

/** 30.7.5:投資ノートのフロントマターと記入漏れ項目。ノートが無ければ`exists:false`。 */
export function fetchResearchNote(ticker: string): Promise<ResearchNoteResponse> {
  return apiFetch(`/api/v1/research/${encodeURIComponent(ticker)}`);
}

/** K-9:その銘柄のLLM定性分析(要約+定性評価)。未生成でも200で空が返る。 */
export function fetchLlmAnalysis(ticker: string): Promise<LlmTickerAnalysisResponse> {
  return apiFetch<LlmTickerAnalysisResponse>(`/api/v1/llm/${encodeURIComponent(ticker)}`);
}

/** K-9:当日ランキングの説明文。`date` 省略時は最新。未生成なら exists=false。 */
export function fetchLlmReport(date?: string): Promise<LlmReportResponse> {
  const query = date ? `?date=${encodeURIComponent(date)}` : "";
  return apiFetch<LlmReportResponse>(`/api/v1/llm/report${query}`);
}

/** K-9:選べるLLMプロバイダと、それぞれ実際に呼べるか(APIキーの有無)。 */
export function fetchLlmProviders(): Promise<LlmProvidersResponse> {
  return apiFetch<LlmProvidersResponse>(`/api/v1/llm/providers`);
}

/** K-9:いま実際に使われるLLM接続の実効値(アクティブなプロファイル + yaml/.env)。 */
export function fetchLlmSettings(): Promise<LlmSettings> {
  return apiFetch<LlmSettings>(`/api/v1/llm/settings`);
}

/** K-9:保存済みの接続プロファイル一覧(APIキー本体は含まない)。 */
export function fetchLlmConnections(): Promise<LlmConnectionsResponse> {
  return apiFetch<LlmConnectionsResponse>(`/api/v1/llm/connections`);
}

/** K-9:接続プロファイルを新規保存。名前は一意。課金は発生しない。 */
export function createLlmConnection(body: LlmConnectionCreate): Promise<LlmConnection> {
  return apiPost<LlmConnection>(`/api/v1/llm/connections`, body);
}

/** K-9:接続プロファイルを編集。`""` で base_url/model/effort をクリア、api_key を削除。 */
export function updateLlmConnection(id: number, body: LlmConnectionUpdate): Promise<LlmConnection> {
  return apiPut<LlmConnection>(`/api/v1/llm/connections/${id}`, body);
}

/** K-9:接続プロファイルを削除。 */
export function deleteLlmConnection(id: number): Promise<void> {
  return apiDelete<void>(`/api/v1/llm/connections/${id}`);
}

/** K-9:このプロファイルをアクティブにする(他は自動で下りる)。 */
export function activateLlmConnection(id: number): Promise<LlmConnection> {
  return apiPost<LlmConnection>(`/api/v1/llm/connections/${id}/activate`);
}

/** K-9:アクティブを解除する(以後は collection.yaml / .env のまま)。 */
export function deactivateLlmConnections(): Promise<LlmConnectionsResponse> {
  return apiPost<LlmConnectionsResponse>(`/api/v1/llm/connections/deactivate`);
}

/**
 * K-9:当日ランキングの説明文をUIから生成する。**課金が発生する。**
 * `confirm: true` が必須(誤爆防止)。サーバ側で30秒レート制限と同時実行ロック。
 */
export function generateLlmReport(body: GenerateReportRequest): Promise<GenerateReportResult> {
  return apiPost<GenerateReportResult>(`/api/v1/llm/report/generate`, body);
}

// ---------------------------------------------------------------------------
// Model v5 (Phase 8). All read-only -- v5 has no write endpoints from the
// UI, matching the shadow-only, append-only backend contract.
// ---------------------------------------------------------------------------

export function fetchV5LatestRun(asOf?: string): Promise<ModelV5Run> {
  const q = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  return apiFetch(`/api/v1/models/v5/runs/latest${q}`);
}

export interface FetchV5ScoresParams {
  objective?: string;
  asOf?: string;
  limit?: number;
  offset?: number;
}

export function fetchV5Scores(params: FetchV5ScoresParams = {}): Promise<ModelV5ScoreListResponse> {
  const q = new URLSearchParams();
  if (params.objective) q.set("objective", params.objective);
  if (params.asOf) q.set("as_of", params.asOf);
  q.set("limit", String(params.limit ?? 50));
  q.set("offset", String(params.offset ?? 0));
  return apiFetch(`/api/v1/models/v5/scores?${q.toString()}`);
}

export function fetchV5ScoreDetail(ticker: string, asOf?: string): Promise<ModelV5ScoreDetail> {
  const q = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  return apiFetch(`/api/v1/models/v5/scores/${encodeURIComponent(ticker)}${q}`);
}

export function fetchV5Objectives(): Promise<ModelV5ObjectivesResponse> {
  return apiFetch(`/api/v1/models/v5/objectives`);
}

export function fetchV5ValidationStatus(): Promise<ModelV5ValidationStatus> {
  return apiFetch(`/api/v1/models/v5/validation-status`);
}
