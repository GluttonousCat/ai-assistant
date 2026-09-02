import { useEffect, useRef, useState } from 'react'
import { platformApi } from './platformApi'
import ResultChart, { buildChartSpec, COL_LABELS, parseNum, fmtDateCol } from './ResultChart'

const EXAMPLES = [
  { icon: '📊', text: '查询贵州茅台的营收' },
  { icon: '📊', text: '看看宁德时代的毛利率' },
  { icon: '📄', text: '解读中芯国际的研报' },
  { icon: '🪙', text: '看看黄金的观点' },
]

// 回答类型徽章: SSE data 事件携带 intent, 前端按类型显示图标
const INTENT_META = {
  query: { icon: '📊', label: '财务数据' },
  compare: { icon: '⚖️', label: '对比分析' },
  report: { icon: '📄', label: '研报解读' },
}

const NAME_COLS = ['stock_name', 'name']
const DATE_COLS = ['end_date', 'ann_date']
const FOLDABLE = [...NAME_COLS, ...DATE_COLS]

function fmtCell(v) {
  if (v === null || v === undefined || v === '') return '—'
  const n = parseNum(v)
  if (n !== null) {
    if (Math.abs(n) >= 1000) return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
    return String(parseFloat(n.toFixed(4))) // 去掉浮点尾噪
  }
  return String(v)
}

// 结果呈现: 可视化优先 (折线/柱状), 不可图示时回退精简表格
// 单值列 (股票名/报告期) 折叠为标题; ts_code 一律隐藏
function resultView(m) {
  const cols = (m.columns || []).filter((c) => c !== 'ts_code')
  if (!m.data?.length) return null

  const distinct = (c) => [...new Set(m.data.map((r) => r[c]).filter((v) => v !== null && v !== undefined))]
  const caption = FOLDABLE
    .filter((c) => cols.includes(c) && distinct(c).length === 1)
    .map((c) => (NAME_COLS.includes(c) ? String(distinct(c)[0]) : fmtDateCol(c, distinct(c)[0])))
    .join(' · ') || null

  const chart = buildChartSpec(m.columns, m.data)
  if (chart) return { caption, chart }

  const show = cols.filter((c) => !(FOLDABLE.includes(c) && distinct(c).length === 1))
  if (show.length === 0) return null
  return { caption, columns: show }
}

// Agent 对话: 自然语言 -> 财务查询 / 研报解读 (SSE 流式输出)
export default function AgentChat() {
  const [messages, setMessages] = useState([]) // {role, text, data?, columns?, stage?, intent?}
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const scrollRef = useRef(null)
  const stickRef = useRef(true) // 用户贴底时跟随新内容, 上翻查看时不打扰

  const scrollToBottom = (smooth = false) => {
    const el = scrollRef.current
    if (!el) return
    stickRef.current = true
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }

  // 流式输出时贴底跟随; 用户上翻后不再强制拉回
  useEffect(() => {
    if (stickRef.current) {
      const el = scrollRef.current
      if (el) el.scrollTop = el.scrollHeight
    }
  }, [messages, busy])

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    stickRef.current = nearBottom
    setAtBottom(nearBottom)
  }

  async function send(text) {
    const q = (text ?? input).trim()
    if (!q || busy) return
    setInput('')
    scrollToBottom()
    setMessages((m) => [...m, { role: 'user', text: q }])
    setBusy(true)

    // 流式消息: text 逐块增长, stage 显示当前阶段
    setMessages((m) => [...m, { role: 'assistant', text: '', stage: '' }])

    const updateLast = (fn) => {
      setMessages((m) => {
        const copy = [...m]
        copy[copy.length - 1] = fn(copy[copy.length - 1])
        return copy
      })
    }

    try {
      await platformApi.chatStream(q, (ev) => {
        if (ev.type === 'stage') {
          updateLast((msg) => {
            // 首个 stage "意图识别: X" 提前确定回答类型 (徽章即时显示)
            const im = /^意图识别:\s*(\w+)/.exec(ev.message || '')
            return { ...msg, stage: ev.message, intent: im ? im[1] : msg.intent }
          })
        } else if (ev.type === 'data') {
          updateLast((msg) => ({
            ...msg,
            intent: ev.intent || msg.intent,
            data: ev.data, columns: ev.columns, rows: ev.rows,
            stage: '',
          }))
        } else if (ev.type === 'delta') {
          updateLast((msg) => ({ ...msg, text: msg.text + ev.text, stage: '' }))
        } else if (ev.type === 'error') {
          updateLast((msg) => ({ ...msg, text: `❌ ${ev.message}`, stage: '' }))
        }
      })
    } catch (err) {
      updateLast((msg) => ({ ...msg, text: `❌ ${err.message}`, stage: '' }))
    } finally {
      updateLast((msg) => ({ ...msg, stage: '' }))
      setBusy(false)
    }
  }

  return (
    <div className="chat-page">
      <div className="chat-window" ref={scrollRef} onScroll={onScroll}>
        {messages.length === 0 && !busy && (
          <div className="chat-empty">
            <div className="chat-empty-icon">✦</div>
            <h3>Alpha Radar · 投研 Agent</h3>
            <p>自然语言查询财务数据 · 解读知识星球研报 · 数据与观点一站获取</p>
            <div className="chat-examples">
              {EXAMPLES.map((ex) => (
                <button key={ex.text} onClick={() => send(ex.text)}>
                  <span className="ex-icon">{ex.icon}</span>{ex.text}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) => {
          const streaming = busy && i === messages.length - 1 && m.role === 'assistant'
          const view = m.role === 'assistant' && m.data?.length ? resultView(m) : null
          return (
            <div key={i} className={`chat-msg ${m.role}`}>
              {m.role === 'assistant' ? (
                <>
                  <div className="chat-avatar">✦</div>
                  <div className="chat-body">
                    {INTENT_META[m.intent] && (
                      <div className={`chat-type-badge ${m.intent}`}>
                        <span className="icon">{INTENT_META[m.intent].icon}</span>
                        {INTENT_META[m.intent].label}
                      </div>
                    )}
                    {m.stage && (
                      <div className="chat-stage">
                        <span className="spinner-dot" /> {m.stage}
                      </div>
                    )}
                    {(m.text || !m.stage) && (
                      <div className={`chat-bubble md${streaming ? ' streaming' : ''}`}>
                        {m.text ? <Markdownish text={m.text} /> : (
                          <div className="chat-bubble typing"><span></span><span></span><span></span></div>
                        )}
                      </div>
                    )}
                    {view && (
                      <div className="chat-table-block">
                        {view.caption && <div className="chat-table-caption">{view.caption}</div>}
                        {view.chart ? (
                          <ResultChart spec={view.chart} />
                        ) : (
                          <div className="chat-table">
                            <table className="grid">
                              <thead>
                                <tr>{view.columns.map((c) => <th key={c}>{COL_LABELS[c] || c}</th>)}</tr>
                              </thead>
                              <tbody>
                                {m.data.map((row, j) => (
                                  <tr key={j}>
                                    {view.columns.map((c) => (
                                      <td key={c}>
                                        {(c === 'end_date' || c === 'ann_date' || c === 'trade_date')
                                          ? fmtDateCol(c, row[c])
                                          : fmtCell(row[c])}
                                      </td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <div className="chat-bubble user">{m.text}</div>
              )}
            </div>
          )
        })}
        <div className="chat-window-pad" />
      </div>

      {!atBottom && (
        <button className="chat-jump" title="回到底部" onClick={() => scrollToBottom(true)}>↓</button>
      )}

      <div className="chat-inputbar">
        <div className="chat-input-shell">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && send()}
            placeholder="例如: 解读中芯国际的研报 / 查询平安银行的净利润"
            disabled={busy}
          />
          <button className="btn" onClick={() => send()} disabled={busy || !input.trim()}>发送</button>
        </div>
      </div>
    </div>
  )
}

// 轻量 markdown: 标题/加粗/列表/段落
function Markdownish({ text }) {
  const lines = (text || '').split('\n')
  return lines.map((line, i) => {
    const t = line.trim()
    if (!t) return <div key={i} style={{ height: 8 }} />
    if (t.startsWith('### ')) return <h4 key={i}>{inline(t.slice(4))}</h4>
    if (t.startsWith('## ')) return <h3 key={i}>{inline(t.slice(3))}</h3>
    if (t.startsWith('# ')) return <h3 key={i}>{inline(t.slice(2))}</h3>
    if (/^[-•*]\s+/.test(t)) return <div key={i} className="md-li">{inline(t.replace(/^[-•*]\s+/, ''))}</div>
    if (/^\d+[.、)]\s+/.test(t)) return <div key={i} className="md-li">{inline(t)}</div>
    return <p key={i}>{inline(t)}</p>
  })
}

function inline(t) {
  const parts = t.split(/(\*\*[^*]+\*\*)/g)
  return parts.map((p, i) =>
    p.startsWith('**') && p.endsWith('**')
      ? <b key={i}>{p.slice(2, -2)}</b>
      : <span key={i}>{p}</span>
  )
}
