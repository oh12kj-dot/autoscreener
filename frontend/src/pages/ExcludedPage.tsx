import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { fetchExcluded } from "../api/client";
import type { ExcludedListResponse } from "../api/types";

const PAGE_SIZE = 50;
const DEFAULT_HORIZON_YEARS = 7;
const DEFAULT_TARGET_MOIC = 10;

export function ExcludedPage() {
  // 29章:規模の上限が目標倍率の関数になったため、この一覧も目標に依存する。
  // ランキング画面と同じ `h` / `m` をURLから読む(ヘッダーのリンクが引き継ぐ)。
  const [searchParams] = useSearchParams();
  const horizonYears = Number(searchParams.get("h") ?? DEFAULT_HORIZON_YEARS);
  const targetMoic = Number(searchParams.get("m") ?? DEFAULT_TARGET_MOIC);
  const isDefaultTarget = horizonYears === DEFAULT_HORIZON_YEARS && targetMoic === DEFAULT_TARGET_MOIC;

  const [reason, setReason] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ExcludedListResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    fetchExcluded({ reason: reason || undefined, limit: PAGE_SIZE, offset, horizonYears, targetMoic })
      .then(setData)
      .finally(() => setLoading(false));
  }, [reason, offset, horizonYears, targetMoic]);

  return (
    <div>
      <h2>
        除外銘柄の確認
        <span className="ranking-target">
          {horizonYears}年で{targetMoic}倍
        </span>
      </h2>
      <p>除外ゲート(15.2)で候補から外れた銘柄を、理由で絞り込んで確認できます(14.16)。</p>
      <p className="target-universe">
        <strong>この一覧は目標によって変わります。</strong>
        規模の上限は目標倍率に追随するため(29章)、
        {isDefaultTarget ? "既定の「7年で10倍」" : `「${horizonYears}年で${targetMoic}倍」`}
        には大きすぎる銘柄が <span className="reason-tag">market_cap_ceiling</span>
        <span className="reason-tag">revenue_ceiling</span> としてここに出ます。
        目標を緩めると、その一部は候補側へ移ります。
      </p>
      <div className="filters">
        <label>
          除外理由でフィルタ
          <input
            value={reason}
            onChange={(e) => {
              setReason(e.target.value);
              setOffset(0);
            }}
            placeholder="例: negative_equity"
          />
        </label>
      </div>

      {loading && <p>読み込み中...</p>}

      {data && (
        <>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>銘柄</th>
                  <th>セクター</th>
                  <th>除外理由</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.ticker}>
                    <td>
                      <Link
                        to={`/candidates/${item.ticker}?h=${horizonYears}&m=${targetMoic}`}
                        className="ticker-link"
                      >
                        <span className="ticker-symbol">{item.ticker}</span>
                        {item.company_name && <span className="company-name-cell">{item.company_name}</span>}
                      </Link>
                    </td>
                    <td>{item.sector ?? "—"}</td>
                    <td>
                      {item.exclusion_reason.map((reason) => (
                        <span key={reason} className="reason-tag">
                          {reason}
                        </span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pagination">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              前へ
            </button>
            <span>
              {offset + 1}〜{Math.min(offset + PAGE_SIZE, data.total)} / {data.total}件
            </span>
            <button disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>
              次へ
            </button>
          </div>
        </>
      )}
    </div>
  );
}
