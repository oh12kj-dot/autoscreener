import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { fetchUsdJpy } from "./api/client";

/**
 * J-10(docs/investment_decision_gap_2026-08-29.md):円換算表示。
 *
 * **やらないこと(30.1.3 のまま)**:税務計算、取得為替レートでの損益計算、
 * 確定申告用の出力。ここにあるのは**表示用の換算だけ**。
 *
 * 通貨の選択は `localStorage` に保存する(per-viewer の便宜。壊れても既定の USD で動く)。
 * USD/JPY レートは `/api/v1/fx/usdjpy`(FRED DEXJPUS → yfinance JPY=X フォールバック)。
 */

type Currency = "USD" | "JPY";

interface CurrencyContextValue {
  currency: Currency;
  setCurrency: (c: Currency) => void;
  /** USD/JPY。取得できないときは null(トグルは無効表示にする)。 */
  rate: number | null;
  rateSource: string | null;
  /** USD 建ての金額を、選択中の通貨の表示文字列にする。 */
  formatMoney: (usd: number | null | undefined) => string;
}

const CurrencyContext = createContext<CurrencyContextValue | null>(null);

const STORAGE_KEY = "tenx.currency";

function readStored(): Currency {
  try {
    return localStorage.getItem(STORAGE_KEY) === "JPY" ? "JPY" : "USD";
  } catch {
    return "USD";
  }
}

export function CurrencyProvider({ children }: { children: ReactNode }) {
  const [currency, setCurrencyState] = useState<Currency>(readStored);
  const [rate, setRate] = useState<number | null>(null);
  const [rateSource, setRateSource] = useState<string | null>(null);

  useEffect(() => {
    fetchUsdJpy()
      .then((r) => {
        setRate(r.rate);
        setRateSource(r.source);
      })
      .catch(() => {
        setRate(null);
        setRateSource(null);
      });
  }, []);

  const setCurrency = useCallback((c: Currency) => {
    setCurrencyState(c);
    try {
      localStorage.setItem(STORAGE_KEY, c);
    } catch {
      /* private mode 等。無視して続行する */
    }
  }, []);

  const formatMoney = useCallback(
    (usd: number | null | undefined): string => {
      if (usd == null || Number.isNaN(usd)) return "—";
      if (currency === "JPY" && rate != null) {
        const jpy = usd * rate;
        if (Math.abs(jpy) >= 1e12) return `¥${(jpy / 1e12).toFixed(2)}兆`;
        if (Math.abs(jpy) >= 1e8) return `¥${(jpy / 1e8).toFixed(2)}億`;
        if (Math.abs(jpy) >= 1e4) return `¥${(jpy / 1e4).toFixed(0)}万`;
        return `¥${jpy.toFixed(0)}`;
      }
      if (Math.abs(usd) >= 1e9) return `$${(usd / 1e9).toFixed(2)}B`;
      if (Math.abs(usd) >= 1e6) return `$${(usd / 1e6).toFixed(1)}M`;
      if (Math.abs(usd) >= 1e3) return `$${(usd / 1e3).toFixed(0)}K`;
      return `$${usd.toFixed(0)}`;
    },
    [currency, rate],
  );

  const value = useMemo(
    () => ({ currency, setCurrency, rate, rateSource, formatMoney }),
    [currency, setCurrency, rate, rateSource, formatMoney],
  );

  return <CurrencyContext.Provider value={value}>{children}</CurrencyContext.Provider>;
}

export function useCurrency(): CurrencyContextValue {
  const ctx = useContext(CurrencyContext);
  if (ctx == null) {
    // Provider の外で呼ばれても落とさない(USD 固定で動く)。
    return {
      currency: "USD",
      setCurrency: () => {},
      rate: null,
      rateSource: null,
      formatMoney: (usd) =>
        usd == null ? "—" : usd >= 1e9 ? `$${(usd / 1e9).toFixed(2)}B` : `$${(usd / 1e6).toFixed(1)}M`,
    };
  }
  return ctx;
}

/** ヘッダーに置く USD / JPY トグル。 */
export function CurrencyToggle() {
  const { currency, setCurrency, rate, rateSource } = useCurrency();
  return (
    <span className="currency-toggle">
      <button
        type="button"
        className={`link-button${currency === "USD" ? " active" : ""}`}
        onClick={() => setCurrency("USD")}
      >
        USD
      </button>
      {" / "}
      <button
        type="button"
        className={`link-button${currency === "JPY" ? " active" : ""}`}
        onClick={() => setCurrency("JPY")}
        disabled={rate == null}
        title={rate == null ? "USD/JPY レートが取得できません(collect-macro 未実行?)" : rateSource ?? ""}
      >
        JPY
      </button>
      {currency === "JPY" && rate != null && (
        <span className="detail-cagr"> @{rate.toFixed(1)}</span>
      )}
    </span>
  );
}
