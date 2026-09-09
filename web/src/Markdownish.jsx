// 轻量 markdown 渲染 (AgentChat / ChainView 共用):
// 标题/加粗/列表/表格/段落 (表格行需连续 | 开头)
export default function Markdownish({ text }) {
  const lines = (text || '').split('\n')
  const out = []
  let i = 0
  let k = 0
  while (i < lines.length) {
    const t = lines[i].trim()
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

// markdown 表格: | a | b | 行 + |---|---| 分隔行
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
  const parts = t.split(/(\*\*[^*]+\*\*)/g)
  return parts.map((p, i) =>
    p.startsWith('**') && p.endsWith('**')
      ? <b key={i}>{p.slice(2, -2)}</b>
      : <span key={i}>{p}</span>
  )
}
