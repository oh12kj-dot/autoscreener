import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CoverageMessage } from "./InvestmentIntelligenceSections";
import type { InvestmentIntelligenceResponse } from "../api/types";

const response = (coverage_status: InvestmentIntelligenceResponse["coverage_status"], reason_code: string | null = null): InvestmentIntelligenceResponse => ({
  ticker: "TEST", as_of: "2026-09-01", coverage_status, reason_code, reason_detail: null, observed_at: null,
  source: null, source_url: null, data_age_days: null, retryable: coverage_status === "collection_failed",
  not_used_in_ranking: true, data: null,
});

describe("CoverageMessage", () => {
  it("renders each meaningful empty state differently", () => {
    const cases: Array<[InvestmentIntelligenceResponse["coverage_status"], string]> = [
      ["not_collected", "未試行・未取得"], ["collected_no_finding", "取得済み・該当なし"],
      ["collection_failed", "取得失敗"], ["not_applicable", "対象外"],
    ];
    for (const [status, label] of cases) {
      const { unmount } = render(<CoverageMessage response={response(status)} />);
      expect(screen.getByText(label, { exact: false })).toBeInTheDocument();
      unmount();
    }
  });

  it("labels missing research input without calling it a collection failure", () => {
    render(<CoverageMessage response={response("not_collected", "user_input_missing")} />);
    expect(screen.getByText(/ユーザー未設定/)).toBeInTheDocument();
  });
});
