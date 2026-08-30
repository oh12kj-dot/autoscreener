import { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { CATEGORIES, entriesByCategory, search } from "../glossary";

/**
 * 用語集(28.18)。
 *
 * このアプリは「株価 = 売上 × 利益率 × マルチプル ÷ 株式数」という恒等式を
 * そのまま計算に使っているため、画面に出る言葉が金融の専門用語で埋まる。
 * 意味が分からないまま順位だけを見るのは、**モデルを信じるか信じないかの
 * 判断材料を持たないまま使う**ということであり、このアプリが一貫して避けようと
 * してきた状態そのものである(14.2「上位デシルでも大半は外れる前提をUI上に
 * 明示すること」と同じ動機)。
 */
export function GlossaryPage() {
  const [query, setQuery] = useState("");
  const location = useLocation();
  const results = useMemo(() => search(query), [query]);
  const matchedIds = useMemo(() => new Set(results.map((e) => e.id)), [results]);
  const isSearching = query.trim().length > 0;

  // URLハッシュ(ツールチップの「くわしく →」)で該当項目へ飛ぶ。
  // React Router 単体ではハッシュへスクロールしないので自前で処理する。
  useEffect(() => {
    if (!location.hash) return;
    const target = document.getElementById(location.hash.slice(1));
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("glossary-entry-flash");
    const timer = window.setTimeout(() => target.classList.remove("glossary-entry-flash"), 1600);
    return () => window.clearTimeout(timer);
  }, [location.hash, isSearching]);

  return (
    <div className="glossary-page">
      <h2>用語集</h2>

      <div className="model-notice">
        <strong>意味が分からない言葉があるまま順位だけを見ないでください。</strong>{" "}
        このアプリは株価を「売上 × 利益率 × マルチプル ÷ 株式数」に分解して計算しているため、
        画面に金融の専門用語が多く出てきます。<em>言葉の意味が分からない状態は、
        モデルを信じてよいかどうかを判断できない状態</em>と同じです。
        画面上で点線の下線が引かれた言葉は、マウスを乗せる(またはクリック・タップする)と
        その場で説明が出ます。
      </div>

      <div className="glossary-search">
        <label>
          用語を探す
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="例: マルチプル、希薄化、CAGR"
          />
        </label>
        {isSearching && (
          <span className="glossary-count">
            {results.length}件
            {results.length === 0 && " —— 別の言い方で探してみてください"}
          </span>
        )}
      </div>

      {!isSearching && (
        <nav className="glossary-toc" aria-label="用語集の目次">
          {CATEGORIES.map((category) => (
            <a key={category.id} href={`#cat-${category.id}`}>
              {category.label}
            </a>
          ))}
        </nav>
      )}

      {CATEGORIES.map((category) => {
        const entries = entriesByCategory(category.id).filter((e) => matchedIds.has(e.id));
        if (entries.length === 0) return null;
        return (
          <section key={category.id} className="glossary-category" id={`cat-${category.id}`}>
            <h3>{category.label}</h3>
            <p className="factor-intro">{category.lead}</p>
            <div className="glossary-entries">
              {entries.map((entry) => (
                <article key={entry.id} id={entry.id} className="glossary-entry">
                  <h4>
                    {entry.term}
                    {entry.aliases && entry.aliases.length > 0 && (
                      <span className="glossary-aliases">{entry.aliases.join(" / ")}</span>
                    )}
                  </h4>
                  <p className="glossary-short">{entry.short}</p>
                  {entry.body.map((paragraph) => (
                    <p key={paragraph.slice(0, 24)} className="glossary-body">
                      {renderEmphasis(paragraph)}
                    </p>
                  ))}
                  {entry.example && (
                    <p className="glossary-example">
                      <span className="glossary-example-label">例</span>
                      {entry.example}
                    </p>
                  )}
                </article>
              ))}
            </div>
          </section>
        );
      })}

      <p className="glossary-footer">
        計算式そのものを知りたい場合は<Link to="/reference">スコアについて</Link>を、
        モデルがどれくらい当たっているかは<Link to="/validation">モデルの検証状況</Link>を
        見てください。
      </p>
    </div>
  );
}

/**
 * `**強調**` を `<strong>` に変える。
 *
 * 用語集の本文には「ここだけは読み飛ばさないでほしい」という箇所がある。
 * Markdownライブラリを持ち込むほどの要求ではないので、この記法だけを扱う。
 */
function renderEmphasis(text: string): React.ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, index) =>
    part.startsWith("**") && part.endsWith("**") ? (
      <strong key={index}>{part.slice(2, -2)}</strong>
    ) : (
      <span key={index}>{part}</span>
    ),
  );
}
