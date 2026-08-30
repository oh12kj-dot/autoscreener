import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { lookup, type GlossaryId } from "../glossary";

/**
 * 専門用語に説明を付けるコンポーネント(28.18)。
 *
 * **hoverだけにしない。** マウスを持っていない利用者(タッチ端末・キーボード
 * 操作)には hover が存在しないため、それだけに頼ると説明が完全に消える。
 * ここではトリガーを `<button>` にして、クリック/Enter でも開くようにしてある。
 *
 * **開閉のハンドラは「ラッパー」に付ける(28.19)。** トリガーのボタンに
 * `onMouseLeave` / `onBlur` を付けていたとき、ツールチップはボタンの兄弟要素
 * なので、**中の「くわしく →」リンクへマウスを動かした瞬間に閉じていた**。
 * さらに、リンクを押そうとすると mousedown → ボタンの blur → ツールチップの
 * アンマウント、の順で処理が進み、mouseup が行き場を失って**クリックが成立
 * しない**。用語集への導線が存在するのに一度も辿れない、という状態だった。
 * ラッパー(ボタンとツールチップの共通の親)を境界にすれば、両方とも起きない。
 *
 * 説明文は `glossary.ts` の1箇所だけに書く。ツールチップと用語集ページで
 * 別々に文章を持つと、片方だけが古くなっても誰も気づけない。
 */

interface TermProps {
  /** `glossary.ts` に実在する id のみ。存在しないidは `tsc` が弾く */
  id: GlossaryId;
  /** 画面上の表記を用語集の見出しと変えたいとき */
  children?: React.ReactNode;
}

export function Term({ id, children }: TermProps) {
  const entry = lookup(id);
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLSpanElement>(null);
  const tooltipId = useId();

  // 外側のクリックと Escape で閉じる。開きっぱなしのツールチップが表の上に
  // 残り続けると、その下のデータが読めなくなる。
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // 用語集に無いidを黙って素通りさせると、説明が付いているつもりの箇所が
  // 無言で普通のテキストになる。型(`GlossaryId`)で防いでいるが、
  // 万一すり抜けたときに開発時だけ気づけるようにしておく。
  if (!entry) {
    if (import.meta.env.DEV) console.warn(`[Term] 用語集に "${id}" がありません`);
    return <>{children ?? id}</>;
  }

  return (
    <span
      className="term-wrapper"
      ref={wrapperRef}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      // フォーカスがラッパーの外へ出たときだけ閉じる。中の「くわしく →」へ
      // Tab で移動しても閉じないようにするため、移動先を見て判定する。
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
      }}
      onFocus={() => setOpen(true)}
    >
      <button
        type="button"
        className="term-trigger"
        aria-describedby={open ? tooltipId : undefined}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {children ?? entry.term}
      </button>
      {open && (
        <span className="term-tooltip" id={tooltipId} role="tooltip">
          <span className="term-tooltip-title">{entry.term}</span>
          <span className="term-tooltip-body">{entry.short}</span>
          <Link to={`/glossary#${entry.id}`} className="term-tooltip-link">
            くわしく →
          </Link>
        </span>
      )}
    </span>
  );
}
