import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';

// Models write LaTeX as \( \) / \[ \] too; normalise to $ / $$ for remark-math.
function normaliseMath(s: string) {
  return s
    .replace(/\\\[([\s\S]+?)\\\]/g, (_m, x) => `$$${x}$$`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_m, x) => `$${x}$`);
}

export function Markdown({ text, className = '' }: { text: string; className?: string }) {
  return (
    <div className={`md ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[[rehypeKatex, { throwOnError: false, strict: false }]]}>
        {normaliseMath(text || '')}
      </ReactMarkdown>
    </div>
  );
}
