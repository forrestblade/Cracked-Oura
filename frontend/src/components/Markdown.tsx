import React from 'react';

/**
 * Minimal dependency-free Markdown renderer for AI chat messages.
 * Supports: headings, bold, italic, inline code, fenced code blocks,
 * bullet/numbered lists, links, paragraphs. No raw HTML injection.
 */

function renderInline(text: string): React.ReactNode[] {
    const nodes: React.ReactNode[] = [];
    // Tokenize: `code`, **bold**, *italic*, [label](url)
    const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)]+\))/g;
    let last = 0;
    let m: RegExpExecArray | null;
    let k = 0;
    while ((m = re.exec(text)) !== null) {
        if (m.index > last) nodes.push(text.slice(last, m.index));
        const tok = m[0];
        if (tok.startsWith('`')) {
            nodes.push(<code key={k++} className="px-1 py-0.5 rounded bg-muted font-mono text-[0.85em]">{tok.slice(1, -1)}</code>);
        } else if (tok.startsWith('**')) {
            nodes.push(<strong key={k++}>{renderInline(tok.slice(2, -2))}</strong>);
        } else if (tok.startsWith('[')) {
            const mm = tok.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
            if (mm) nodes.push(<a key={k++} href={mm[2]} target="_blank" rel="noreferrer" className="underline text-primary">{mm[1]}</a>);
            else nodes.push(tok);
        } else {
            nodes.push(<em key={k++}>{renderInline(tok.slice(1, -1))}</em>);
        }
        last = m.index + tok.length;
    }
    if (last < text.length) nodes.push(text.slice(last));
    return nodes;
}

export function Markdown({ text }: { text: string }) {
    const blocks: React.ReactNode[] = [];
    const lines = (text || '').split('\n');
    let i = 0;
    let k = 0;

    while (i < lines.length) {
        const line = lines[i];

        // Fenced code block
        if (line.trim().startsWith('```')) {
            const buf: string[] = [];
            i++;
            while (i < lines.length && !lines[i].trim().startsWith('```')) buf.push(lines[i++]);
            i++; // closing fence
            blocks.push(
                <pre key={k++} className="my-2 p-3 rounded-md bg-muted overflow-x-auto text-xs font-mono">
                    {buf.join('\n')}
                </pre>
            );
            continue;
        }

        // Heading
        const h = line.match(/^(#{1,4})\s+(.*)$/);
        if (h) {
            const sizes = ['text-lg', 'text-base', 'text-sm', 'text-sm'];
            blocks.push(
                <div key={k++} className={`${sizes[h[1].length - 1]} font-semibold mt-3 mb-1`}>
                    {renderInline(h[2])}
                </div>
            );
            i++;
            continue;
        }

        // Bullet / numbered list
        if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
            const items: { text: string; ordered: boolean }[] = [];
            while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
                const mm = lines[i].match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
                items.push({ text: mm ? mm[1] : lines[i], ordered: /^\s*\d+\./.test(lines[i]) });
                i++;
            }
            const ordered = items[0]?.ordered;
            const List = ordered ? 'ol' : 'ul';
            blocks.push(
                <List key={k++} className={`my-1.5 pl-5 space-y-1 ${ordered ? 'list-decimal' : 'list-disc'}`}>
                    {items.map((it, j) => <li key={j}>{renderInline(it.text)}</li>)}
                </List>
            );
            continue;
        }

        // Blank line
        if (line.trim() === '') { i++; continue; }

        // Paragraph (merge consecutive non-empty, non-special lines)
        const buf: string[] = [];
        while (i < lines.length && lines[i].trim() !== '' &&
               !/^\s*([-*]|\d+\.)\s+/.test(lines[i]) &&
               !/^#{1,4}\s+/.test(lines[i]) &&
               !lines[i].trim().startsWith('```')) {
            buf.push(lines[i++]);
        }
        blocks.push(<p key={k++} className="my-1.5 leading-relaxed">{renderInline(buf.join(' '))}</p>);
    }

    return <div className="text-sm">{blocks}</div>;
}
