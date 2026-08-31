import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { CurrencyToggle } from "../currency";
import { ErrorBoundary } from "./ErrorBoundary";

export function Layout() {
  // ErrorBoundaryはクラスコンポーネントで、一度エラー状態になると
  // 子(Outletの中身)が変わっただけでは自動的に回復しない。pathnameを
  // keyにして、ルートが変わるたびに強制的に作り直す(=エラー状態をリセット
  // する)。そうしないと、クラッシュ後に別画面へ移動しても同じエラー画面が
  // 表示され続ける。
  const location = useLocation();
  // 29章:選んだ目標(何年で何倍)は画面をまたいで保つ。除外銘柄一覧は目標に
  // よって内容が変わる(規模の上限が目標倍率の関数)ので、ランキングで
  // 「3年で3倍」を選んだまま除外一覧へ移ったのに既定の目標の一覧が出る、
  // という食い違いを防ぐ。
  const target = new URLSearchParams();
  const current = new URLSearchParams(location.search);
  for (const key of ["h", "m"]) {
    const value = current.get(key);
    if (value) target.set(key, value);
  }
  const search = target.toString() ? `?${target.toString()}` : "";
  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">TX</span>
          <span className="brand-name">TENX</span>
        </div>
        <nav>
          <NavLink to={{ pathname: "/", search }} end>
            ランキング
          </NavLink>
          <NavLink to={{ pathname: "/watchlist", search }}>監視リスト</NavLink>
          <NavLink to="/rank-changes">順位変動</NavLink>
          <NavLink to={{ pathname: "/excluded", search }}>除外銘柄</NavLink>
          <NavLink to="/positions">保有銘柄</NavLink>
          <NavLink to="/calendar">カレンダー</NavLink>
          <NavLink to="/alerts">アラート</NavLink>
          <NavLink to="/macro">マクロ</NavLink>
          <NavLink to="/llm-report">日次レポート</NavLink>
          <NavLink to="/glossary">用語集</NavLink>
          <NavLink to="/reference">スコアについて</NavLink>
          <NavLink to="/validation">モデルの検証状況</NavLink>
          <NavLink to="/data-coverage">データカバレッジ</NavLink>
          {/* 14.15:日常的に見る画面ではない。平常時は見なくてよく、異常時にだけ
              見に行く画面として、意図的にナビ末尾に置く(§6)。 */}
          <NavLink to="/pipeline">日次ジョブ</NavLink>
        </nav>
        <CurrencyToggle />
      </header>
      <main className="app-main">
        <ErrorBoundary key={location.pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
      {/* 30.9.3 / J-5:免責。全画面のフッターに常時表示し、検証状況へ導線を張る。 */}
      <footer className="app-disclaimer">
        <p>
          このツールは投資助言ではありません。表示される順位・確率・分位点は特定のモデルの仮定に基づく
          <strong>推定値</strong>であり、その多くは実測で検証されていません。最終的な投資判断と結果の責任は
          利用者にあります。モデルがいまどこまで検証できているかは
          <Link to="/validation"> モデルの検証状況</Link> で必ず確認してください。
        </p>
      </footer>
    </div>
  );
}
