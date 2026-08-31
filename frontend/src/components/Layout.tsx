import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { CurrencyToggle } from "../currency";
import { ErrorBoundary } from "./ErrorBoundary";

type NavItem = {
  label: string;
  to: string;
  preserveTarget?: boolean;
};

type NavGroup = {
  label: string;
  items: NavItem[];
};

const NAV_GROUPS: NavGroup[] = [
  {
    label: "投資候補",
    items: [
      { label: "ランキング", to: "/", preserveTarget: true },
      { label: "監視リスト", to: "/watchlist", preserveTarget: true },
      { label: "順位変動", to: "/rank-changes" },
      { label: "除外銘柄", to: "/excluded", preserveTarget: true },
    ],
  },
  {
    label: "意思決定",
    items: [
      { label: "保有銘柄", to: "/positions" },
      { label: "カレンダー", to: "/calendar" },
      { label: "アラート", to: "/alerts" },
      { label: "マクロ", to: "/macro" },
      { label: "日次レポート", to: "/llm-report" },
    ],
  },
  {
    label: "検証・根拠",
    items: [
      { label: "モデルの検証状況", to: "/validation" },
      { label: "データカバレッジ", to: "/data-coverage" },
      { label: "スコアについて", to: "/reference" },
      { label: "用語集", to: "/glossary" },
    ],
  },
  {
    label: "運用",
    items: [{ label: "日次ジョブ", to: "/pipeline" }],
  },
];

const PAGE_TITLES: Record<string, { eyebrow: string; title: string }> = {
  "/": { eyebrow: "Screening", title: "投資候補ランキング" },
  "/watchlist": { eyebrow: "Watch", title: "監視リスト" },
  "/rank-changes": { eyebrow: "Momentum", title: "順位変動" },
  "/excluded": { eyebrow: "Universe", title: "除外銘柄" },
  "/positions": { eyebrow: "Portfolio", title: "保有銘柄" },
  "/calendar": { eyebrow: "Catalysts", title: "カレンダー" },
  "/alerts": { eyebrow: "Risk Monitor", title: "アラート" },
  "/macro": { eyebrow: "Environment", title: "マクロ環境" },
  "/llm-report": { eyebrow: "Daily Brief", title: "日次レポート" },
  "/glossary": { eyebrow: "Reference", title: "用語集" },
  "/reference": { eyebrow: "Methodology", title: "スコアについて" },
  "/validation": { eyebrow: "Validation", title: "モデルの検証状況" },
  "/pipeline": { eyebrow: "Operations", title: "日次ジョブ" },
  "/data-coverage": { eyebrow: "Data Quality", title: "データカバレッジ" },
};

function pageContext(pathname: string) {
  if (pathname.startsWith("/candidates/")) {
    const ticker = decodeURIComponent(pathname.split("/")[2] ?? "");
    return { eyebrow: "Decision Dossier", title: ticker ? `${ticker} 投資判断` : "銘柄詳細" };
  }
  return PAGE_TITLES[pathname] ?? { eyebrow: "TENX", title: "投資判断ワークスペース" };
}

function pageClass(pathname: string) {
  if (pathname === "/") return "page-ranking";
  if (pathname.startsWith("/candidates/")) return "page-ticker-detail";
  return `page-${pathname.replace(/^\//, "").replaceAll("/", "-") || "home"}`;
}

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
  const context = pageContext(location.pathname);

  return (
    <div className="app-shell app-shell--workspace">
      <aside className="workspace-sidebar">
        <Link className="workspace-brand" to={{ pathname: "/", search }} aria-label="TENX ランキングへ">
          <span className="brand-mark">TX</span>
          <span className="workspace-brand-copy">
            <span className="brand-name">TENX</span>
            <span className="workspace-brand-subtitle">Investment Workspace</span>
          </span>
        </Link>

        <nav className="workspace-nav" aria-label="メインナビゲーション">
          {NAV_GROUPS.map((group) => (
            <div className="workspace-nav-group" key={group.label}>
              <span className="workspace-nav-label">{group.label}</span>
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  className="workspace-nav-link"
                  to={item.preserveTarget ? { pathname: item.to, search } : item.to}
                  end={item.to === "/"}
                >
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <div className="workspace-sidebar-note">
          <strong>判断前に確認</strong><br />
          順位だけでなく、下振れ・生存確率・流動性・一次情報・検証状況を合わせて確認してください。
          <br />
          <Link to="/validation">検証状況を見る →</Link>
        </div>
      </aside>

      <div className="workspace-frame">
        <header className="workspace-topbar">
          <div className="workspace-topbar-context">
            <span className="workspace-topbar-eyebrow">{context.eyebrow}</span>
            <h1>{context.title}</h1>
          </div>
          <div className="workspace-topbar-actions">
            <CurrencyToggle />
          </div>
        </header>

        <main className={`app-main workspace-main ${pageClass(location.pathname)}`}>
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
    </div>
  );
}
