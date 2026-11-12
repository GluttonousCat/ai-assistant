import { memo } from 'react'

// Lightweight markdown renderer (shared by AgentChat / ChainView):
// headings/bold/lists/tables/paragraphs (table rows must be consecutive lines starting with |)
function Markdownish({ text }) {
  const lines = (text || '').split('\n')
  const out = []
  let i = 0
  let k = 0
  while (i < lines.length) {
    const t = lines[i].trim()
    if (t.startsWith('```')) {
      const language = t.slice(3).trim()
      const code = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith('```')) code.push(lines[i++])
      if (i < lines.length) i++
      out.push(<div key={k++}>{language && <div className="md-code-label">{language}</div>}<pre className="md-code"><code>{code.join('\n')}</code></pre></div>)
      continue
    }
    if (t.startsWith('|')) {
      const block = []
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        block.push(lines[i].trim())
        i++
      }
      out.push(<MdTable key={k++} block={block} />)
      continue
    }
    if (!t) { out.push(<div key={k++} style={{ height: 8 }} />); i++; continue }
    if (t.startsWith('### ')) { out.push(<h4 key={k++}>{inline(t.slice(4))}</h4>); i++; continue }
    if (t.startsWith('## ')) { out.push(<h3 key={k++}>{inline(t.slice(3))}</h3>); i++; continue }
    if (t.startsWith('# ')) { out.push(<h3 key={k++}>{inline(t.slice(2))}</h3>); i++; continue }
    if (/^[-•*]\s+/.test(t)) { out.push(<div key={k++} className="md-li">{inline(t.replace(/^[-•*]\s+/, ''))}</div>); i++; continue }
    if (/^\d+[.、)]\s+/.test(t)) { out.push(<div key={k++} className="md-li">{inline(t)}</div>); i++; continue }
    out.push(<p key={k++}>{inline(t)}</p>)
    i++
  }
  return out
}

// Markdown table: rows like "| a | b |" plus a "|---|---|" separator row
function MdTable({ block }) {
  const rows = block
    .filter((l) => !/^\|?[\s:|-]+\|?$/.test(l))
    .map((l) => l.replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim()))
  if (!rows.length) return null
  const [head, ...body] = rows
  return (
    <div className="chat-table">
      <table className="grid">
        <thead><tr>{head.map((c, j) => <th key={j}>{inline(c)}</th>)}</tr></thead>
        <tbody>
          {body.map((r, j) => (
            <tr key={j}>{head.map((_, cj) => <td key={cj}>{inline(r[cj] ?? '')}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function inline(t) {
  return t.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g).map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) return <b key={i}>{part.slice(2, -2)}</b>
    if (part.startsWith('`') && part.endsWith('`')) return <code key={i}>{part.slice(1, -1)}</code>
    const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part)
    if (link && /^https?:\/\//i.test(link[2])) return <a key={i} href={link[2]} target="_blank" rel="noopener noreferrer">{link[1]}</a>
    return <span key={i}>{part}</span>
  })
}

export default memo(Markdownish)
