/** K-9:LLMが返すMarkdownの最小レンダラ。
 *
 * **markdownライブラリを入れない。** 扱うのは自前のプロンプトが指定した
 * 見出し(`###`)・箇条書き(`-`)・段落だけであり、そのために依存を1つ増やす
 * 理由が無い。加えて、多くのMarkdownライブラリは生HTMLの通過を既定で許すか、
 * オプション1つで許してしまう——ここに流れてくるのは**モデルが生成した文字列**
 * なので、その経路は最初から作らないほうがよい。
 *
 * このコンポーネントは `dangerouslySetInnerHTML` を使わない。Reactが既定で
 * 文字列をエスケープするため、モデルの出力がマークアップとして解釈されることは
 * 構造上起こらない。
 */

interface Block {
  kind: "heading" | "paragraph" | "list";
  text?: string;
  items?: string[];
}

/** 行を見出し・箇条書き・段落のブロックに畳む。 */
function parseBlocks(markdown: string): Block[] {
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length > 0) {
      blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list.length > 0) {
      blocks.push({ kind: "list", items: list });
      list = [];
    }
  };

  for (const rawLine of markdown.split("\n")) {
    const line = rawLine.trim();
    if (line === "") {
      flushParagraph();
      flushList();
      continue;
    }
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ kind: "heading", text: heading[1] });
      continue;
    }
    const bullet = /^[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushParagraph();
      list.push(bullet[1]);
      continue;
    }
    flushList();
    paragraph.push(line);
  }
  flushParagraph();
  flushList();
  return blocks;
}

/** `**強調**` だけをインライン装飾として扱う。それ以外は素のテキストのまま出す。 */
function renderInline(text: string) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
      <strong key={i}>{part.slice(2, -2)}</strong>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

export function LlmMarkdown({ content }: { content: string }) {
  return (
    <div className="llm-markdown">
      {parseBlocks(content).map((block, i) => {
        if (block.kind === "heading") {
          return <h4 key={i}>{renderInline(block.text ?? "")}</h4>;
        }
        if (block.kind === "list") {
          return (
            <ul key={i}>
              {(block.items ?? []).map((item, j) => (
                <li key={j}>{renderInline(item)}</li>
              ))}
            </ul>
          );
        }
        return <p key={i}>{renderInline(block.text ?? "")}</p>;
      })}
    </div>
  );
}
