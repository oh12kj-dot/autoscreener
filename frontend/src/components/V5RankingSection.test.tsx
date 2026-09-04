import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { V5RankingSection } from "./V5RankingSection";
import type {
  ModelV5ObjectivesResponse,
  ModelV5ScoreListResponse,
  ModelV5ValidationStatus,
} from "../api/types";

/**
 * WP-C(docs/racr_wp_c_api_ui_2026-09-04.md)の中心要件を固定するテスト:
 * `p_permanent_loss` / `expected_max_drawdown` が `null` の行は
 * "— 未推定" と表示され、**"0%"や"0.0%"としては絶対に描画されない**。
 * この誤読(0%=リスク無し、と読める表示)が、このUI作業パッケージ全体の
 * 存在理由なので、回帰させないための直接テスト。
 */

vi.mock("../api/client", () => {
  const objectives: ModelV5ObjectivesResponse = {
    default_objective: "ten_bagger",
    objectives: [
      { name: "ten_bagger", description: "P(target)" },
      { name: "risk_adjusted_compounding", description: "RACR" },
    ],
  };

  const scores: ModelV5ScoreListResponse = {
    run: {
      run_id: "11111111-2222-3333-4444-555555555555",
      model_version: "v5",
      config_hash: "test-config-hash",
      as_of: "2026-09-04",
      mode: "shadow",
      status: "succeeded",
      population_count: 1,
      started_at: "2026-09-04T00:00:00Z",
      finished_at: "2026-09-04T00:01:00Z",
      metrics: null,
      warnings: [],
    },
    selected_objective: "ten_bagger",
    total: 1,
    objective_computed_for_run: true,
    items: [
      {
        rank: 1,
        ticker: "ZZUNAVAIL",
        selected_objective: "ten_bagger",
        objective_value: 0.02,
        confidence: 0.7,
        warnings: [],
        distribution: {
          contract_version: "v5.racr2",
          status: "available",
          distribution_family: "failure_atom_plus_scenario_lognormal_mixture",
          source_model_version: "v4_structural_seed",
          target_moic: 10,
          p_moic_below_0_5: 0.3,
          p_moic_below_1_0: 0.4,
          p_moic_2x: 0.3,
          p_moic_3x: 0.2,
          p_moic_5x: 0.1,
          p_moic_10x: 0.02,
          p_target: 0.02,
          expected_moic: 2.0,
          median_moic: 1.5,
          expected_cagr: 0.15,
          median_cagr: 0.1,
          expected_shortfall_10pct: -0.5,
          p10_moic: 0.2,
          p25_moic: 0.5,
          p50_moic: 1.5,
          p75_moic: 2.5,
          p90_moic: 4.0,
          survival_probability: 0.9,
          acquisition_probability: null,
          model_confidence: 0.7,
          expected_moic_given_loss: 0.5,
          reliability_sigma_multiplier: 1.0,
          reliability_left_tail_extra: 0.0,
          scenarios: [],
          ce_cagr: 0.08,
          ce_cagr_failure_floor: 0.01,
          p_cagr_above_15: 0.2,
          p_cagr_above_20: 0.1,
          p_cagr_above_25: 0.05,
          expected_shortfall_10pct_log: -0.2,
          expected_shortfall_10pct_log_given_survival: -0.15,
          p_terminal_wealth_below_0_5: 0.3,
          // The field under test: always null today. Must never render as 0%.
          p_permanent_loss: null,
          p_permanent_loss_unavailable_reason: "competing_risk_model_not_implemented",
          expected_max_drawdown: null,
          expected_max_drawdown_unavailable_reason: "path_simulation_not_implemented",
          p_mdd_above_30: null,
          p_mdd_above_30_unavailable_reason: "path_simulation_not_implemented",
          p_mdd_above_50: null,
          p_mdd_above_50_unavailable_reason: "path_simulation_not_implemented",
          p_mdd_above_70: null,
          p_mdd_above_70_unavailable_reason: "path_simulation_not_implemented",
          recovery_time_median: null,
          recovery_time_median_unavailable_reason: "path_simulation_not_implemented",
        },
      },
    ],
  };

  const validationStatus: ModelV5ValidationStatus = {
    decision: "CONTINUE_SHADOW",
    decision_entry_date: "2026-09-01",
    champion_model: "v4",
    challenger_model: "v5",
    challenger_mode: "shadow",
    evaluation_dates_count: 0,
    evaluation_date_range: null,
    realized_forward_validation_count: 0,
    unsupported_historical_features: [],
    latest_run: null,
    not_for_production: true,
    warnings: [],
  };

  return {
    fetchV5Objectives: vi.fn().mockResolvedValue(objectives),
    fetchV5Scores: vi.fn().mockResolvedValue(scores),
    fetchV5ValidationStatus: vi.fn().mockResolvedValue(validationStatus),
  };
});

describe("V5RankingSection", () => {
  it("renders permanent loss and MDD as unavailable, never as 0%", async () => {
    render(
      <MemoryRouter>
        <V5RankingSection />
      </MemoryRouter>
    );

    await waitFor(() => expect(screen.getByText("ZZUNAVAIL")).toBeInTheDocument());

    // The required rendering: an explicit "unavailable" marker for both
    // permanent loss and expected max drawdown (2 cells for this one row).
    const unavailableMarkers = screen.getAllByText("— 未推定");
    expect(unavailableMarkers.length).toBe(2);

    // ...each carrying its machine-readable reason as a native tooltip,
    // in plain Japanese -- not the raw unavailable_reason code, and never
    // silently dropped.
    const reasons = unavailableMarkers.map((el) => el.title);
    expect(reasons.every((title) => title.length > 0)).toBe(true);
    expect(reasons.some((title) => title.includes("破綻・上場廃止の原因別"))).toBe(true);
    expect(reasons.some((title) => title.includes("価格経路シミュレーション"))).toBe(true);
    expect(reasons.every((title) => title !== "competing_risk_model_not_implemented")).toBe(true);
    expect(reasons.every((title) => title !== "path_simulation_not_implemented")).toBe(true);
  });

  it("shows an explicit message, not an empty ranking table, when the run predates the objective", async () => {
    const client = await import("../api/client");
    (client.fetchV5Scores as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      run: {
        run_id: "00000000-0000-0000-0000-000000000000",
        model_version: "v5",
        config_hash: "pre-racr",
        as_of: "2026-08-01",
        mode: "shadow",
        status: "succeeded",
        population_count: 1,
        started_at: "2026-08-01T00:00:00Z",
        finished_at: "2026-08-01T00:01:00Z",
        metrics: null,
        warnings: [],
      },
      selected_objective: "risk_adjusted_compounding",
      total: 0,
      objective_computed_for_run: false,
      items: [],
    } satisfies ModelV5ScoreListResponse);

    render(
      <MemoryRouter initialEntries={["/?objective=risk_adjusted_compounding"]}>
        <V5RankingSection />
      </MemoryRouter>
    );

    await waitFor(() =>
      expect(screen.getByText(/まだ計算していません/)).toBeInTheDocument()
    );
    // Must not be conflated with the ordinary "no candidates" message.
    expect(screen.queryByText("該当する候補がありません。")).not.toBeInTheDocument();
  });
});
